"""Operational surface for Stage 0 (SEAT-006; REQ-040, REQ-044).

`/readyz` and `/metrics` do not exist yet (SEAT-009, SEAT-050) and are deliberately
not asserted on here. What exists is `/healthz`, the access log and its exemptions,
the startup line, and the unhandled-exception counter.
"""

from __future__ import annotations

import json
import socket
import statistics
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.constants import (
    ACCESS_LOG_EXEMPT_PATHS,
    HEALTH_STATUS_OK,
    REDACTED,
    SERVICE_NAME,
    UNMATCHED_ROUTE_LABEL,
    Header,
    LogEvent,
)
from app.core.metrics import unhandled_exceptions_total
from tests.conftest import (
    HEALTHZ_BUDGET_MS,
    HEALTHZ_SAMPLES,
    PROBE_BOOM_PATH,
    PROBE_DECLINE_PATH,
    PROBE_OK_PATH,
    PROBE_PARAM_TEMPLATE,
    LogCapture,
    is_uuid,
)

HEALTHZ = "/healthz"


def _dsn_password(dsn: str) -> str | None:
    """The password component, parsed by hand.

    `urllib.parse` cannot be used on a *redacted* DSN: `[redacted]` in the userinfo
    looks like an IPv6 literal, and `urlsplit(...).password` raises `ValueError`.
    """
    userinfo, separator, _ = dsn.partition("://")[2].partition("@")
    if not separator or ":" not in userinfo:
        return None
    return userinfo.split(":", 1)[1]


def _counter_value(route: str) -> float:
    for metric in unhandled_exceptions_total.collect():
        for sample in metric.samples:
            if sample.labels.get("route") == route and sample.name.endswith("_total"):
                return float(sample.value)
    return 0.0


# ---------------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------------


async def test_healthz_returns_the_liveness_payload(
    client: httpx.AsyncClient, resolved_settings: Settings
) -> None:
    response = await client.get(HEALTHZ)

    assert response.status_code == 200
    assert response.json() == {
        "status": HEALTH_STATUS_OK,
        "service": SERVICE_NAME,
        "version": resolved_settings.service_version,
    }


async def test_healthz_echoes_a_request_id_despite_being_log_exempt(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(HEALTHZ)

    assert is_uuid(response.headers.get(Header.REQUEST_ID))


async def test_healthz_opens_no_outbound_connection(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-040: liveness touches no dependency.

    Proven by making any outbound socket connection fail rather than by inspecting
    imports — a healthcheck that reaches the database restarts a healthy process
    during a database blip.
    """

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("/healthz attempted an outbound connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    response = await client.get(HEALTHZ)

    assert response.status_code == 200


async def test_healthz_latency_is_under_the_budget(client: httpx.AsyncClient) -> None:
    """REQ-040: single-digit milliseconds. Budget from `HEALTHZ_BUDGET_MS`, not inlined."""
    for _ in range(5):
        await client.get(HEALTHZ)

    samples_ms: list[float] = []
    for _ in range(HEALTHZ_SAMPLES):
        started = time.perf_counter()
        response = await client.get(HEALTHZ)
        samples_ms.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200

    samples_ms.sort()
    p95 = samples_ms[min(len(samples_ms) - 1, int(len(samples_ms) * 0.95))]
    median = statistics.median(samples_ms)
    assert p95 <= HEALTHZ_BUDGET_MS, (
        f"/healthz p95 {p95:.3f}ms exceeds the {HEALTHZ_BUDGET_MS}ms budget "
        f"(median {median:.3f}ms, max {samples_ms[-1]:.3f}ms, n={len(samples_ms)})"
    )


async def test_healthz_produces_no_access_log_line(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    """A platform probe every few seconds would otherwise dominate log volume."""
    response = await client.get(HEALTHZ)

    assert response.status_code == 200
    assert log_capture.where(event=LogEvent.HTTP_REQUEST.value) == []
    assert log_capture.entries == [], f"unexpected lines for {HEALTHZ}: {log_capture.entries}"


async def test_a_non_exempt_route_does_produce_an_access_log_line(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    """Control for the test above: the exemption, not a broken capture, is what silences it."""
    await client.get(PROBE_OK_PATH)

    assert len(log_capture.where(event=LogEvent.HTTP_REQUEST.value)) == 1


def test_the_exempt_path_set_is_the_documented_one() -> None:
    assert frozenset({"/healthz", "/readyz", "/metrics"}) == ACCESS_LOG_EXEMPT_PATHS


# ---------------------------------------------------------------------------------
# The access log line
# ---------------------------------------------------------------------------------


async def test_access_log_line_has_the_documented_fields(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get(PROBE_OK_PATH)

    line = log_capture.one(event=LogEvent.HTTP_REQUEST.value, level="info")
    assert {
        "ts",
        "level",
        "event",
        "request_id",
        "service",
        "version",
        "method",
        "path",
        "route",
        "status",
        "duration_ms",
    } <= set(line)
    assert line["method"] == "GET"
    assert line["path"] == PROBE_OK_PATH
    assert line["route"] == PROBE_OK_PATH
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float | int)
    assert line["duration_ms"] >= 0


async def test_access_log_route_is_the_template_not_the_concrete_path(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    """A burst over 20,000 ids must group into one series, not explode cardinality."""
    concrete = PROBE_PARAM_TEMPLATE.replace("{show_id}", "abc-123")

    await client.get(concrete)

    line = log_capture.one(event=LogEvent.HTTP_REQUEST.value)
    assert line["path"] == concrete
    assert line["route"] == PROBE_PARAM_TEMPLATE


async def test_an_unmatched_path_is_logged_under_one_label(
    client: httpx.AsyncClient, log_capture: LogCapture
) -> None:
    await client.get("/__probe/does-not-exist")

    assert log_capture.one(event=LogEvent.HTTP_REQUEST.value)["route"] == UNMATCHED_ROUTE_LABEL


# ---------------------------------------------------------------------------------
# Startup line
# ---------------------------------------------------------------------------------


async def test_startup_line_reports_the_resolved_config_with_secrets_redacted(
    probe_app: FastAPI,
    app_lifespan: Callable[[FastAPI], Any],
    log_capture: LogCapture,
    resolved_settings: Settings,
) -> None:
    async with app_lifespan(probe_app):
        pass

    startup = log_capture.one(event=LogEvent.STARTUP.value, level="info")
    config = startup["config"]
    assert config["jwt_secret"] == REDACTED
    assert config["admin_password"] == REDACTED
    assert config["database_url"] == REDACTED

    # The formatter neutralises both DSN fields before this point, so scanning for a
    # value starting with "postgres" iterates zero times and proves nothing. Assert the
    # composed path directly: the real password must not appear anywhere on the line.
    for name in ("database_url", "test_database_url"):
        assert config[name] == REDACTED, f"{name} was not redacted: {config[name]!r}"
    # The composed path, not just the field: the real password must appear nowhere on the
    # line. The previous version scanned for a value starting with "postgres", which the
    # formatter has already replaced, so it iterated zero times and proved nothing.
    assert "topsecret" not in json.dumps(startup), "a DSN password reached the startup line"

    for secret in (
        resolved_settings.jwt_secret.get_secret_value(),
        resolved_settings.admin_password.get_secret_value(),
    ):
        assert secret not in log_capture.text, "a secret reached the startup log line"

    assert log_capture.one(event=LogEvent.SHUTDOWN.value)


# ---------------------------------------------------------------------------------
# unhandled_exceptions_total
# ---------------------------------------------------------------------------------


async def test_unhandled_exception_increments_the_counter_once(
    client: httpx.AsyncClient,
) -> None:
    """REQ-048 measures this counter, so a double count would misreport a burst."""
    before = _counter_value(PROBE_BOOM_PATH)

    await client.get(PROBE_BOOM_PATH)

    assert _counter_value(PROBE_BOOM_PATH) == before + 1


async def test_a_healthy_request_does_not_increment_the_counter(
    client: httpx.AsyncClient,
) -> None:
    before = _counter_value(PROBE_OK_PATH)

    await client.get(PROBE_OK_PATH)
    await client.get(HEALTHZ)

    assert _counter_value(PROBE_OK_PATH) == before


async def test_a_domain_decline_does_not_increment_the_counter(
    client: httpx.AsyncClient,
) -> None:
    """A 409 is the correct answer, not a fault; counting it would hide real faults."""
    before = _counter_value(PROBE_DECLINE_PATH)

    response = await client.get(PROBE_DECLINE_PATH)

    assert response.status_code == 409
    assert _counter_value(PROBE_DECLINE_PATH) == before
