"""Response bodies shared across route modules."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
