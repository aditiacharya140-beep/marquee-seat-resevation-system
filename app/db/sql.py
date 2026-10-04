"""The effective-status expressions, each defined exactly once (ADR-012, ADR-017).

Nothing rewrites a lapsed hold, so stored status is not the authority for one. Every
reader and the claim itself must agree on what a lapsed hold is; a second copy of any
of these that drifts is the most likely way this service fails reconciliation.

The three seat fragments are one rule stated three ways: a seat is ACTIVE or it is
CLAIMABLE, never both, and EFFECTIVE_STATUS reports CLAIMABLE as 'available'.
"""

from typing import Final

SEAT_ACTIVE: Final = "(status = 'confirmed' OR (status = 'held' AND hold_expires_at > now()))"

SEAT_CLAIMABLE: Final = "(status = 'available' OR (status = 'held' AND hold_expires_at <= now()))"

SEAT_EFFECTIVE_STATUS: Final = """CASE
    WHEN status = 'confirmed' THEN 'confirmed'
    WHEN status = 'held' AND hold_expires_at > now() THEN 'held'
    ELSE 'available'
END"""

RESERVATION_EFFECTIVE_STATUS: Final = """CASE
    WHEN status = 'held' AND hold_expires_at <= now() THEN 'expired'
    ELSE status
END"""
