"""Typed settings. Every tunable in the service is declared here and nowhere else.

The field set is the configuration table of mds/13-deployment.md, which `.env.example`
mirrors. Secrets have no default: a service that boots with a built-in signing key is
worse than one that refuses to boot.
"""

import re
from typing import Annotated, Any, Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.constants import DSN_REDACTED, REDACTED, LogLevel

#: Kept back from the pool for the migration that runs at boot and an operator's own
#: session: a pool that could take every connection would lock both out.
RESERVED_NON_POOL_CONNECTIONS: Final = 1


def redact_dsn(dsn: str) -> str:
    """Strip credentials from a connection string so it is safe to log.

    Total by construction: a log helper that can raise turns a diagnostic into
    an outage. An unparseable DSN redacts to the placeholder rather than
    propagating ValueError into the startup path.
    """
    try:
        parts = urlsplit(dsn)
        _host, _port = parts.hostname, parts.port
    except ValueError:
        return REDACTED
    parts = urlsplit(dsn)
    if parts.hostname is None:
        return REDACTED
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    if parts.username:
        # Not REDACTED: its brackets would parse as an IPv6 literal in userinfo,
        # making the logged DSN unparseable by urlsplit.
        netloc = f"{parts.username}:{DSN_REDACTED}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(validation_alias="DATABASE_URL", min_length=1)
    test_database_url: str | None = None

    db_pool_min: int = 5
    db_pool_max: int = 20
    db_acquire_timeout_seconds: int = 10
    db_statement_timeout_ms: int = 5000
    db_lock_timeout_ms: int = 2000
    db_timeout_margin_ms: int = 1000
    db_idle_txn_timeout_ms: int = 10000
    readyz_timeout_seconds: float = 2.0
    #: Read from the server, not guessed: Homebrew Postgres 16 defaults to 100 and the
    #: test suite draws from the same ceiling (LEARN-003). Managed instances differ.
    db_server_max_connections: int = 100

    allowed_event_kinds: Annotated[frozenset[str], NoDecode]
    default_event_kind: str
    default_currency: str

    jwt_secret: SecretStr = Field(validation_alias="JWT_SECRET", min_length=32)
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 604800
    guest_token_ttl_seconds: int = 3600
    admin_email: str = Field(validation_alias="ADMIN_EMAIL", min_length=1)
    admin_password: SecretStr = Field(validation_alias="ADMIN_PASSWORD", min_length=12)
    password_min_length: int = 12
    password_hash_workers: int = 4

    default_per_user_limit: int = 4
    default_hold_ttl_seconds: int = 120
    max_hold_ttl_seconds: int = 900
    max_seats_per_show: int = 5000
    max_seat_label_length: int = 16
    page_size_default: int = 20
    page_size_max: int = 100

    rate_limit_enabled: bool = True
    rate_limit_reserve: str = "120/10s"
    rate_limit_read: str = "300/10s"
    rate_limit_auth: str = "10/60s"
    rate_limit_guest: str = "60/60s"
    rate_limit_admin: str = "30/60s"
    rate_limit_max_buckets: int = 100000
    #: Proxies this deployment itself runs in front of the service. 0 ignores
    #: X-Forwarded-For entirely and uses the socket peer.
    rate_limit_trusted_proxy_hops: int = 1

    gauge_max_shows: int = 50

    idempotency_wait_ms: int = 2000
    idempotency_poll_interval_ms: int = 50
    idempotency_stale_seconds: int = 30
    idempotency_retention_hours: int = 48
    idempotency_key_max_length: int = 255
    idempotency_retry_after_seconds: int = 1

    log_level: LogLevel = LogLevel.INFO
    log_queue_max: int = 10000
    service_version: str = "0.1.0"

    @field_validator("allowed_event_kinds", mode="before")
    @classmethod
    def _split_event_kinds(cls, value: Any) -> Any:
        """Accept a comma-separated list; pydantic-settings would otherwise expect JSON."""
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def check_relationships(self) -> "Settings":
        """Relationship checks from mds/13-deployment.md.

        A misconfiguration that only manifests as a 503 under load is far more
        expensive than one that fails at boot.
        """
        if self.guest_token_ttl_seconds <= self.max_hold_ttl_seconds:
            raise ValueError(
                f"GUEST_TOKEN_TTL_SECONDS ({self.guest_token_ttl_seconds}) must exceed "
                f"MAX_HOLD_TTL_SECONDS ({self.max_hold_ttl_seconds}): otherwise a guest's "
                "token expires before the longest permitted hold can be confirmed"
            )

        pool_ceiling = self.db_server_max_connections - RESERVED_NON_POOL_CONNECTIONS
        if self.db_pool_max > pool_ceiling:
            raise ValueError(
                f"DB_POOL_MAX ({self.db_pool_max}) exceeds the usable server ceiling "
                f"({pool_ceiling} = DB_SERVER_MAX_CONNECTIONS "
                f"{self.db_server_max_connections} less one kept back for migrations): "
                "a pool larger than the server can serve converts queueing into refusals"
            )

        if self.db_acquire_timeout_seconds * 1000 <= self.db_statement_timeout_ms:
            raise ValueError(
                f"DB_ACQUIRE_TIMEOUT_SECONDS ({self.db_acquire_timeout_seconds}) must exceed "
                f"DB_STATEMENT_TIMEOUT_MS ({self.db_statement_timeout_ms}) expressed in "
                "seconds: a request waiting for the pool would otherwise be refused while "
                "the queue ahead of it was still about to drain"
            )

        dsns = (("DATABASE_URL", self.database_url), ("TEST_DATABASE_URL", self.test_database_url))
        for name, dsn in dsns:
            if dsn is None:
                continue
            try:
                _ = urlsplit(dsn).port
            except ValueError as exc:
                raise ValueError(
                    f"{name} is not a parseable connection string ({exc}): percent-encode "
                    "any '/', '[' or ':' in the password"
                ) from None

        if self.default_hold_ttl_seconds > self.max_hold_ttl_seconds:
            raise ValueError(
                f"DEFAULT_HOLD_TTL_SECONDS ({self.default_hold_ttl_seconds}) exceeds "
                f"MAX_HOLD_TTL_SECONDS ({self.max_hold_ttl_seconds}): every hold taken at the "
                "default would breach the clamp it is validated against, and the guest-token "
                "check below would pass while never actually holding"
            )

        if self.db_pool_min > self.db_pool_max:
            raise ValueError(
                f"DB_POOL_MIN ({self.db_pool_min}) exceeds DB_POOL_MAX ({self.db_pool_max}): "
                "the pool would be rejected at creation, naming neither variable"
            )

        margin_floor = self.db_lock_timeout_ms + self.db_timeout_margin_ms
        if self.db_statement_timeout_ms < margin_floor:
            raise ValueError(
                f"DB_STATEMENT_TIMEOUT_MS ({self.db_statement_timeout_ms}) must be at least "
                f"DB_LOCK_TIMEOUT_MS ({self.db_lock_timeout_ms}) plus DB_TIMEOUT_MARGIN_MS "
                f"({self.db_timeout_margin_ms}) = {margin_floor}: otherwise a contended claim "
                "is cancelled as a statement timeout before its lock timeout can fire, so every "
                "hot-seat decline arrives as a 503 instead of a 409"
            )

        for name in ("reserve", "read", "auth", "guest", "admin"):
            value = getattr(self, f"rate_limit_{name}")
            if not re.fullmatch(r"[1-9]\d*/[1-9]\d*s", value):
                raise ValueError(
                    f"RATE_LIMIT_{name.upper()} ({value}) must look like 120/10s: "
                    "a request count, a slash, and a window in seconds"
                )

        if self.default_event_kind not in self.allowed_event_kinds:
            raise ValueError(
                f"DEFAULT_EVENT_KIND ({self.default_event_kind}) is not in ALLOWED_EVENT_KINDS "
                f"({sorted(self.allowed_event_kinds)}): the default must be permitted"
            )

        return self

    def redacted_summary(self) -> dict[str, Any]:
        """Resolved configuration, safe to log at startup."""
        summary: dict[str, Any] = {}
        for name, value in self:
            if isinstance(value, SecretStr):
                summary[name] = REDACTED
            elif name.endswith("database_url") and isinstance(value, str):
                summary[name] = redact_dsn(value)
            elif isinstance(value, frozenset):
                summary[name] = sorted(value)
            else:
                summary[name] = value
        return summary


class ConfigurationError(RuntimeError):
    """The environment cannot produce a usable configuration."""


def load_settings() -> Settings:
    """Resolve settings, or refuse the boot with a message that names the variables.

    pydantic's own error renders `input_value`, which for a settings model is the whole
    environment payload — including the secrets. Re-raised with `from None` so a boot
    failure cannot print a signing key or a password.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # every field is supplied by the environment
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'relationship'}: "
            f"{error['msg'].removeprefix('Value error, ')}"
            for error in exc.errors()
        )
        raise ConfigurationError(f"invalid configuration: {problems}") from None


#: Resolved at import, so a missing secret refuses the boot rather than failing the
#: first request.
settings = load_settings()
