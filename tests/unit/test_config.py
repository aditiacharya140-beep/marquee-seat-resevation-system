"""Startup configuration (SEAT-002, left to the tester by mds/12-testing-and-burst.md).

A misconfiguration that only manifests as a 503 under load is far more expensive than
one that fails at boot, so every check here asserts two things: that the bad
environment is refused, and that the refusal **names the offending variables**. A
rejection with an unreadable message costs the same outage.

`Settings` reads `.env` by default. Every test here disables that, so a developer's
local file can neither satisfy a deliberately missing variable nor contradict one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import pytest

from app.core.config import (
    RESERVED_NON_POOL_CONNECTIONS,
    ConfigurationError,
    Settings,
    load_settings,
    redact_dsn,
)
from app.core.constants import DSN_REDACTED, REDACTED

#: A configuration that satisfies every relationship check, mirroring `.env.example`.
#: Each test perturbs exactly one thing, so a failure localises.
BASE_ENV: Final[dict[str, str]] = {
    "DATABASE_URL": "postgresql://user:topsecret@db.internal:5432/seatres",
    "TEST_DATABASE_URL": "postgresql://user:topsecret@db.internal:5432/seatres_test",
    "DB_POOL_MIN": "5",
    "DB_POOL_MAX": "20",
    "DB_ACQUIRE_TIMEOUT_SECONDS": "10",
    "DB_STATEMENT_TIMEOUT_MS": "5000",
    "DB_LOCK_TIMEOUT_MS": "2000",
    "DB_TIMEOUT_MARGIN_MS": "1000",
    "DB_IDLE_TXN_TIMEOUT_MS": "10000",
    "DB_SERVER_MAX_CONNECTIONS": "100",
    "ALLOWED_EVENT_KINDS": "cinema,concert",
    "DEFAULT_EVENT_KIND": "cinema",
    "DEFAULT_CURRENCY": "INR",
    "JWT_SECRET": "unit-test-signing-key-padded-for-min-len",
    "ACCESS_TOKEN_TTL_SECONDS": "900",
    "REFRESH_TOKEN_TTL_SECONDS": "604800",
    "GUEST_TOKEN_TTL_SECONDS": "3600",
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_PASSWORD": "unit-test-admin-password",
    "DEFAULT_PER_USER_LIMIT": "4",
    "DEFAULT_HOLD_TTL_SECONDS": "120",
    "MAX_HOLD_TTL_SECONDS": "900",
    "LOG_LEVEL": "info",
    "SERVICE_VERSION": "0.1.0",
}

#: Settings with no default. Absent, the service must refuse to boot rather than
#: inventing a signing key or an admin credential.
REQUIRED_SETTINGS: Final = (
    "DATABASE_URL",
    "JWT_SECRET",
    "ADMIN_EMAIL",
    "ADMIN_PASSWORD",
    "ALLOWED_EVENT_KINDS",
    "DEFAULT_EVENT_KIND",
    "DEFAULT_CURRENCY",
)

SECRET_VALUES: Final = (
    BASE_ENV["JWT_SECRET"],
    BASE_ENV["ADMIN_PASSWORD"],
    "topsecret",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A hermetic environment: no `.env`, no inherited variable, only `BASE_ENV`."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in [*BASE_ENV, *(field.upper() for field in Settings.model_fields)]:
        monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(BASE_ENV)


def _load(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> Settings:
    for name, value in overrides.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return load_settings()


def _refusal(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> str:
    with pytest.raises(ConfigurationError) as caught:
        _load(monkeypatch, **overrides)
    return str(caught.value)


# ---------------------------------------------------------------------------------
# The control: the baseline must load
# ---------------------------------------------------------------------------------


def test_the_baseline_environment_loads(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, every rejection below could be rejecting for the wrong reason."""
    settings = _load(monkeypatch)

    assert settings.database_url == BASE_ENV["DATABASE_URL"]
    assert settings.jwt_secret.get_secret_value() == BASE_ENV["JWT_SECRET"]
    assert settings.db_pool_max == 20


# ---------------------------------------------------------------------------------
# Required values
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", REQUIRED_SETTINGS)
def test_a_missing_required_setting_refuses_the_boot(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    message = _refusal(monkeypatch, **{missing: None})

    assert missing.lower() in message.lower(), f"the refusal does not name {missing}: {message}"


@pytest.mark.parametrize("missing", REQUIRED_SETTINGS)
def test_a_missing_required_setting_never_prints_a_secret(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """pydantic renders `input_value`, which for a settings model is the whole
    environment. `load_settings` re-raises `from None` precisely to prevent that."""
    message = _refusal(monkeypatch, **{missing: None})

    for secret in SECRET_VALUES:
        if secret == BASE_ENV.get(missing):
            continue
        assert secret not in message, f"the boot failure printed {secret!r}: {message}"


def test_an_empty_required_setting_is_refused_as_well(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DATABASE_URL=` in a deploy template is a likelier mistake than its absence."""
    assert "database_url" in _refusal(monkeypatch, DATABASE_URL="").lower()


# ---------------------------------------------------------------------------------
# Relationship checks (mds/13-deployment.md)
# ---------------------------------------------------------------------------------


def test_guest_token_must_outlive_the_longest_permitted_hold(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _refusal(monkeypatch, GUEST_TOKEN_TTL_SECONDS="900", MAX_HOLD_TTL_SECONDS="900")

    assert "GUEST_TOKEN_TTL_SECONDS" in message
    assert "MAX_HOLD_TTL_SECONDS" in message


def test_a_guest_token_shorter_than_the_hold_is_refused(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _refusal(monkeypatch, GUEST_TOKEN_TTL_SECONDS="300", MAX_HOLD_TTL_SECONDS="900")

    assert "GUEST_TOKEN_TTL_SECONDS" in message


def test_a_pool_wider_than_the_server_ceiling_is_refused(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LEARN-003: an oversized pool surfaces as connection errors that look like
    application defects, so pool sizing is a correctness concern, not tuning."""
    message = _refusal(monkeypatch, DB_POOL_MAX="100", DB_SERVER_MAX_CONNECTIONS="100")

    assert "DB_POOL_MAX" in message
    assert "DB_SERVER_MAX_CONNECTIONS" in message


def test_the_pool_ceiling_reserves_the_audit_writers_connection(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ceiling = 40 - RESERVED_NON_POOL_CONNECTIONS

    at_ceiling = _load(monkeypatch, DB_SERVER_MAX_CONNECTIONS="40", DB_POOL_MAX=str(ceiling))
    assert at_ceiling.db_pool_max == ceiling

    assert "DB_POOL_MAX" in _refusal(
        monkeypatch, DB_SERVER_MAX_CONNECTIONS="40", DB_POOL_MAX=str(ceiling + 1)
    )


def test_pool_acquire_must_outlast_a_statement(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _refusal(monkeypatch, DB_ACQUIRE_TIMEOUT_SECONDS="2", DB_STATEMENT_TIMEOUT_MS="5000")

    assert "DB_ACQUIRE_TIMEOUT_SECONDS" in message
    assert "DB_STATEMENT_TIMEOUT_MS" in message


def test_statement_timeout_must_clear_the_lock_timeout_by_the_margin(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-027: reversed, every hot-seat decline arrives as a 503 instead of a 409."""
    message = _refusal(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="2500",
        DB_LOCK_TIMEOUT_MS="2000",
        DB_TIMEOUT_MARGIN_MS="1000",
        DB_ACQUIRE_TIMEOUT_SECONDS="10",
    )

    assert "DB_STATEMENT_TIMEOUT_MS" in message
    assert "DB_LOCK_TIMEOUT_MS" in message
    assert "DB_TIMEOUT_MARGIN_MS" in message


def test_statement_timeout_exactly_at_the_margin_floor_is_permitted(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check is `>=`; asserting the boundary stops it drifting to `>` unnoticed."""
    settings = _load(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="3000",
        DB_LOCK_TIMEOUT_MS="2000",
        DB_TIMEOUT_MARGIN_MS="1000",
        DB_ACQUIRE_TIMEOUT_SECONDS="10",
    )

    assert settings.db_statement_timeout_ms == 3000


def test_one_millisecond_below_the_margin_floor_is_refused(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "DB_STATEMENT_TIMEOUT_MS" in _refusal(
        monkeypatch,
        DB_STATEMENT_TIMEOUT_MS="2999",
        DB_LOCK_TIMEOUT_MS="2000",
        DB_TIMEOUT_MARGIN_MS="1000",
        DB_ACQUIRE_TIMEOUT_SECONDS="10",
    )


def test_a_default_event_kind_outside_the_allowed_set_is_refused(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _refusal(monkeypatch, ALLOWED_EVENT_KINDS="cinema", DEFAULT_EVENT_KIND="theatre")

    assert "DEFAULT_EVENT_KIND" in message
    assert "ALLOWED_EVENT_KINDS" in message


# ---------------------------------------------------------------------------------
# ALLOWED_EVENT_KINDS is configuration, not an enum (ADR-025)
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cinema,concert", {"cinema", "concert"}),
        (" cinema , concert ", {"cinema", "concert"}),
        ("cinema,,concert,", {"cinema", "concert"}),
        ("cinema", {"cinema"}),
        ("cinema,concert,sports", {"cinema", "concert", "sports"}),
    ],
)
def test_allowed_event_kinds_parses_a_comma_separated_list(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str, expected: set[str]
) -> None:
    settings = _load(monkeypatch, ALLOWED_EVENT_KINDS=raw, DEFAULT_EVENT_KIND="cinema")

    assert settings.allowed_event_kinds == frozenset(expected)


def test_a_new_vertical_needs_no_code_change(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _load(
        monkeypatch, ALLOWED_EVENT_KINDS="bus_route,stadium", DEFAULT_EVENT_KIND="bus_route"
    )

    assert settings.default_event_kind == "bus_route"
    assert "stadium" in settings.allowed_event_kinds


# ---------------------------------------------------------------------------------
# The redacted summary
# ---------------------------------------------------------------------------------


def test_redacted_summary_hides_every_secret(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _load(monkeypatch).redacted_summary()

    assert summary["jwt_secret"] == REDACTED
    assert summary["admin_password"] == REDACTED
    rendered = repr(summary)
    for secret in (BASE_ENV["JWT_SECRET"], BASE_ENV["ADMIN_PASSWORD"]):
        assert secret not in rendered


def test_redacted_summary_strips_dsn_credentials(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _load(monkeypatch).redacted_summary()

    for key in ("database_url", "test_database_url"):
        assert "topsecret" not in summary[key], summary[key]
        assert DSN_REDACTED in summary[key]
        assert "db.internal:5432" in summary[key]
        assert REDACTED not in summary[key], "a bracketed placeholder breaks urlsplit"


def test_redacted_summary_covers_every_field(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field added without a thought about logging must still be reported."""
    settings = _load(monkeypatch)

    assert set(settings.redacted_summary()) == set(Settings.model_fields)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://user:topsecret@db.internal:5432/seatres",
            "postgresql://user:redacted@db.internal:5432/seatres",
        ),
        (
            "postgresql://db.internal:5432/seatres",
            "postgresql://db.internal:5432/seatres",
        ),
        (
            "postgresql://user:topsecret@db.internal/seatres?sslmode=require",
            "postgresql://user:redacted@db.internal/seatres",
        ),
        ("not-a-dsn", REDACTED),
        ("", REDACTED),
    ],
)
def test_redact_dsn(dsn: str, expected: str) -> None:
    assert redact_dsn(dsn) == expected


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:topsecret@db.internal:5432/seatres",
        "postgresql://user:topsecret@db.internal/seatres?sslmode=require",
    ],
)
def test_redacted_dsn_is_still_parseable(dsn: str) -> None:
    """The placeholder must not contain brackets.

    "[redacted]" in the userinfo reads as an IPv6 literal, so urlsplit raises on
    the result and anything downstream of the logged DSN breaks.
    """
    redacted = redact_dsn(dsn)
    assert urlsplit(redacted).hostname == "db.internal"
    assert "topsecret" not in redacted


def test_redact_dsn_drops_the_query_string(env: dict[str, str]) -> None:
    """A query string can carry a password, so it is removed rather than filtered."""
    redacted = redact_dsn("postgresql://u:p@h:5432/d?password=topsecret&sslmode=require")

    assert "topsecret" not in redacted
    assert "?" not in redacted


# ---------------------------------------------------------------------------------
# Field hygiene
# ---------------------------------------------------------------------------------


def test_secrets_are_declared_as_secret_types(env: dict[str, str]) -> None:
    """A `str` secret would print in any traceback that renders the model."""
    for name in ("jwt_secret", "admin_password"):
        annotation: Any = Settings.model_fields[name].annotation
        assert annotation is not str, f"{name} is a plain str"
        assert "Secret" in str(annotation), f"{name} is not a secret type: {annotation}"


def test_a_secret_does_not_render_in_the_models_repr(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _load(monkeypatch)

    rendered = repr(settings)
    assert BASE_ENV["JWT_SECRET"] not in rendered
    assert BASE_ENV["ADMIN_PASSWORD"] not in rendered


def test_every_documented_variable_is_declared() -> None:
    """`.env.example` and `Settings` must not diverge.

    Nothing else couples them: deleting a field from `Settings` left the whole suite
    green, so a documented setting could silently stop existing.
    """
    documented = {
        line.split("=", 1)[0].strip().lower()
        for line in Path(".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    declared = set(Settings.model_fields)
    assert documented == declared, (
        f"only in .env.example: {sorted(documented - declared)}; "
        f"only in Settings: {sorted(declared - documented)}"
    )
