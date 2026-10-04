"""Prometheus collectors, declared once.

Labels come from bounded sets only: never a user id, a seat label or a concrete path.
`unhandled_exceptions_total` is the direct measurement of REQ-048 (ADR-016): it must
stay at zero through a burst, and only the catch-all handler increments it.
"""

from prometheus_client import Counter, Gauge

unhandled_exceptions_total = Counter(
    "unhandled_exceptions_total",
    "Exceptions that reached the catch-all handler",
    ["route"],
)

reservations_confirmed_total = Counter(
    "reservations_confirmed_total",
    "Reservations that reached confirmed, by a direct reserve or by confirming a hold",
)
reservations_held_total = Counter(
    "reservations_held_total",
    "Reserves that opted into a hold",
)
reservations_declined_total = Counter(
    "reservations_declined_total",
    "Reserve requests that did not create a reservation, by reason",
    ["reason"],
)
reservations_cancelled_total = Counter(
    "reservations_cancelled_total",
    "Holds released by an explicit cancel",
)
superseded_claims_closed_total = Counter(
    "superseded_claims_closed_total",
    "Claim rows closed because a lapsed hold's seat was re-claimed (ADR-019)",
)

seats_available = Gauge(
    "seats_available",
    "Seats a claim would succeed on right now, computed at scrape time",
    ["show_id"],
)
