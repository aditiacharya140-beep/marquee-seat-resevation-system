"""Response bodies shared across route modules."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class DependencyCheck(BaseModel):
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, DependencyCheck]
