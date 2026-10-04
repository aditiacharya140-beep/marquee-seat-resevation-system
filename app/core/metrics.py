"""Prometheus collectors, declared once.

The full catalogue of mds/10-observability.md lands with SEAT-050. This counter exists
from Stage 0 because it is the direct measurement of REQ-048 (ADR-016): it must stay
at zero through a burst, and only the catch-all handler increments it.
"""

from prometheus_client import Counter

unhandled_exceptions_total = Counter(
    "unhandled_exceptions_total",
    "Exceptions that reached the catch-all handler",
    ["route"],
)
