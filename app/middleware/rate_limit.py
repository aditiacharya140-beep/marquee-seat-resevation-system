"""Per-identity token buckets (mds/07-middleware.md, REQ-047).

Keyed by **principal** wherever a valid token is present, and by client address only
before authentication. That is what keeps an on-sale rush from being throttled: twenty
thousand buyers behind one address are twenty thousand principals. The one place an
address is the only thing available is guest issuance, and that is the ceiling that
bounds how many principals — and so how many per-user seat limits — one client can mint.

Buckets are per process: with N instances the effective ceiling is N times the
configured one. This is abuse protection, not a quota, and no correctness property
depends on it (ADR-007, RISK-003).
"""

import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

import orjson
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import security
from app.core.config import settings
from app.core.constants import (
    ACCESS_LOG_EXEMPT_PATHS,
    ADMIN_PAGE_PATH,
    INDEX_PATH,
    SCOPE_REQUEST_ID,
    STATIC_URL_PREFIX,
    TOKEN_TYPE_BEARER,
    Header,
    RouteClass,
)
from app.core.context import set_outcome_code
from app.core.error_codes import ErrorCode
from app.core.errors import AppError, RateLimitError
from app.core.metrics import rate_limited_total

_CEILING = re.compile(r"^(?P<requests>[1-9]\d*)/(?P<seconds>[1-9]\d*)s$")


@dataclass(frozen=True, slots=True)
class Ceiling:
    requests: int
    seconds: int

    @property
    def refill_per_second(self) -> float:
        return self.requests / self.seconds


@lru_cache
def parse_ceiling(value: str) -> Ceiling:
    """`"120/10s"` is a bucket of 120 that refills over 10 seconds."""
    match = _CEILING.match(value)
    if match is None:
        raise ValueError(f"rate limit {value!r} is not of the form <requests>/<seconds>s")
    return Ceiling(requests=int(match["requests"]), seconds=int(match["seconds"]))


def classify(method: str, path: str) -> RouteClass | None:
    """From the method and path alone: this runs before routing, so that a throttled
    request costs a bucket check and nothing else."""
    if path in ACCESS_LOG_EXEMPT_PATHS:
        return None
    if path in (INDEX_PATH, ADMIN_PAGE_PATH) or path.startswith(f"{STATIC_URL_PREFIX}/"):
        return None
    if path == "/auth/guest":
        return RouteClass.GUEST
    if method == "POST" and path.startswith("/auth/"):
        return RouteClass.AUTH
    if method == "POST" and path == "/shows":
        return RouteClass.ADMIN
    if method == "POST":
        return RouteClass.RESERVE
    return RouteClass.READ


def client_address(scope: Scope, headers: Headers) -> str:
    """The address the nearest trusted proxy saw.

    Entries a client puts in `X-Forwarded-For` arrive on the left and each proxy
    appends on the right, so the entry `RATE_LIMIT_TRUSTED_PROXY_HOPS` from the right
    is the last one this deployment's own infrastructure wrote. Reading the leftmost
    would let any client pick its own bucket by sending the header itself.
    """
    hops = settings.rate_limit_trusted_proxy_hops
    forwarded = [part.strip() for part in headers.get("x-forwarded-for", "").split(",")]
    forwarded = [part for part in forwarded if part]
    if hops > 0 and len(forwarded) >= hops:
        return forwarded[-hops]
    client = scope.get("client")
    return client[0] if client else "unknown"


def _principal(headers: Headers) -> str | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != TOKEN_TYPE_BEARER or not token:
        return None
    try:
        # Verified, not merely decoded: an unverified subject would let a client name
        # a fresh bucket on every request.
        return str(security.verify_access_token(token.strip()).user_id)
    except AppError:
        return None


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


@dataclass(frozen=True, slots=True)
class _Decision:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Bounded, least-recently-used first out: a flood of distinct identities evicts
        # idle buckets instead of growing memory without limit.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        route_class = classify(scope["method"], scope["path"])
        if route_class is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        address = client_address(scope, headers)
        by_address = route_class in (RouteClass.GUEST, RouteClass.AUTH)
        principal = None if by_address else _principal(headers)
        identity = f"user:{principal}" if principal else f"ip:{address}"
        ceiling = parse_ceiling(getattr(settings, f"rate_limit_{route_class.value}"))
        decision = self._take(f"{route_class.value}|{identity}", ceiling)
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        # The caller's own address, so a misconfigured proxy-hop count is visible from
        # outside: it would show an internal address, shared by every client.
        limited_by = "principal" if principal else f"address {address}"
        await self._reject(scope, send, route_class, ceiling, decision, limited_by)

    def _take(self, key: str, ceiling: Ceiling) -> _Decision:
        now = time.monotonic()
        bucket = self._buckets.pop(key, None) or _Bucket(float(ceiling.requests), now)
        bucket.tokens = min(
            float(ceiling.requests),
            bucket.tokens + (now - bucket.updated) * ceiling.refill_per_second,
        )
        bucket.updated = now
        allowed = bucket.tokens >= 1
        if allowed:
            bucket.tokens -= 1
        self._buckets[key] = bucket
        while len(self._buckets) > settings.rate_limit_max_buckets:
            self._buckets.popitem(last=False)
        return _Decision(
            allowed=allowed,
            remaining=int(bucket.tokens),
            retry_after=0
            if allowed
            else math.ceil((1 - bucket.tokens) / ceiling.refill_per_second),
        )

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        route_class: RouteClass,
        ceiling: Ceiling,
        decision: _Decision,
        limited_by: str,
    ) -> None:
        rate_limited_total.labels(route_class=route_class.value).inc()
        set_outcome_code(ErrorCode.RATE_LIMITED.value)
        error = RateLimitError(
            ErrorCode.RATE_LIMITED,
            details={"route_class": route_class.value, "limited_by": limited_by},
        )
        body = orjson.dumps(error.envelope(scope.get(SCOPE_REQUEST_ID)))
        response_headers = {
            "content-type": "application/json",
            "content-length": str(len(body)),
            Header.RETRY_AFTER: str(decision.retry_after),
            Header.RATELIMIT_LIMIT: str(ceiling.requests),
            Header.RATELIMIT_REMAINING: str(decision.remaining),
            Header.RATELIMIT_RESET: str(int(time.time()) + decision.retry_after),
        }
        await send(
            {
                "type": "http.response.start",
                "status": error.http_status,
                "headers": [
                    (name.lower().encode(), value.encode())
                    for name, value in response_headers.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
