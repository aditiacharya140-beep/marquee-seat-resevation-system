"""Reserve, confirm and cancel: the transaction boundaries the correctness argument
in mds/04-concurrency-and-atomicity.md depends on.

A reserve is two transactions. T1 claims the idempotency key and commits at once, so a
concurrent duplicate sees it. T2 is the claim itself — quota lock, limit check, seat
claim, superseded-row closure, reservation, key completion — atomic as one unit.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from app.core import metrics
from app.core.config import settings
from app.core.constants import (
    IDEMPOTENCY_OPERATION_RESERVE,
    PRINTABLE_PATTERN,
    DeclineReason,
    Header,
    IdempotencyState,
    LogEvent,
    ReservationStatus,
    ShowStatus,
)
from app.core.context import note
from app.core.error_codes import REGISTRY, ErrorCode
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.session import acquire, transaction
from app.domain.models import Page, Principal, Reservation, ReserveOutcome, Show
from app.helpers.pagination import page_of, parse_cursor
from app.repositories import idempotency_repo, reservation_repo, seat_repo, show_repo
from app.schemas.reservations import ReservationResponse
from app.utils.canonical_json import fingerprint

logger = get_logger(__name__)


async def reserve(
    principal: Principal,
    show_id: UUID,
    labels: list[str],
    *,
    header_key: str | None,
    body_key: str | None,
    hold_ttl_seconds: int | None,
) -> ReserveOutcome:
    note(show_id=str(show_id), seat_labels=sorted(labels), idempotency_key=header_key or body_key)
    try:
        outcome = await _reserve(
            principal, show_id, sorted(labels), _resolve_key(header_key, body_key), hold_ttl_seconds
        )
    except AppError as error:
        if not REGISTRY[error.code].fault:
            metrics.reservations_declined_total.labels(reason=_decline_reason(error)).inc()
        raise
    if outcome.replayed:
        metrics.reservations_declined_total.labels(
            reason=DeclineReason.IDEMPOTENT_REPLAY.value
        ).inc()
    return outcome


def _decline_reason(error: AppError) -> str:
    reason = (error.details or {}).get("reason")
    return reason if isinstance(reason, str) else error.code.value.lower()


def _resolve_key(header_key: str | None, body_key: str | None) -> str:
    if header_key and body_key and header_key != body_key:
        raise ValidationError(
            ErrorCode.VALIDATION_ERROR,
            message="Idempotency-Key header and idempotency_key body field differ",
        )
    key = header_key or body_key
    if (
        not key
        or len(key) > settings.idempotency_key_max_length
        or not re.fullmatch(PRINTABLE_PATTERN, key)
    ):
        raise ValidationError(
            ErrorCode.VALIDATION_ERROR,
            message="A printable idempotency key is required, in the Idempotency-Key header "
            "or the body",
            details={"max_length": settings.idempotency_key_max_length},
        )
    return key


def _fingerprint(show_id: UUID, labels: list[str], hold_ttl_seconds: int | None) -> str:
    """Operation and show come from the route, so a key cannot escape its scope by
    omitting a field (ADR-021). An absent TTL is omitted, not null: "confirm now" and
    "hold for N" are different operations."""
    request: dict[str, Any] = {
        "op": IDEMPOTENCY_OPERATION_RESERVE,
        "show_id": str(show_id),
        "seats": labels,
    }
    if hold_ttl_seconds is not None:
        request["hold_ttl_seconds"] = hold_ttl_seconds
    return fingerprint(request)


async def _reserve(
    principal: Principal,
    show_id: UUID,
    labels: list[str],
    key: str,
    hold_ttl_seconds: int | None,
) -> ReserveOutcome:
    request_fingerprint = _fingerprint(show_id, labels, hold_ttl_seconds)

    async def try_claim_key(conn: Any) -> UUID | None:
        return await idempotency_repo.try_claim(
            conn,
            user_id=principal.user_id,
            key=key,
            scope=f"{IDEMPOTENCY_OPERATION_RESERVE}:{show_id}",
            fingerprint=request_fingerprint,
            retention_hours=settings.idempotency_retention_hours,
        )

    # T1. No explicit transaction: each statement commits as it completes.
    async with acquire() as conn:
        show = await show_repo.get_show(conn, show_id)
        if show is None:
            raise NotFoundError(ErrorCode.SHOW_NOT_FOUND)
        if show.status is not ShowStatus.ON_SALE:
            raise ConflictError(ErrorCode.SHOW_NOT_ON_SALE)
        if len(labels) > show.per_user_limit:
            raise ValidationError(
                ErrorCode.VALIDATION_ERROR,
                message="More seats requested than the per-user limit for this show",
                details={"limit": show.per_user_limit},
            )
        key_id = await try_claim_key(conn)

    if key_id is None:
        resolved = await _resolve_duplicate(
            principal.user_id, key, request_fingerprint, try_claim_key
        )
        if isinstance(resolved, ReserveOutcome):
            return resolved
        key_id = resolved

    return await _claim(principal, show, labels, hold_ttl_seconds, key_id)


async def _resolve_duplicate(
    user_id: UUID,
    key: str,
    request_fingerprint: str,
    try_claim_key: Callable[[Any], Awaitable[UUID | None]],
) -> ReserveOutcome | UUID:
    """Someone else owns this key. Replay their success, take over if they are gone,
    or wait a bounded time for them to finish.

    The connection is released before every sleep (ADR-026). A waiter that held one
    across the wait budget would, a few hundred duplicates deep, exhaust the pool and
    turn retries into exactly the 503s the reserve path must never produce.
    """
    deadline = time.monotonic() + settings.idempotency_wait_ms / 1000
    while True:
        async with acquire() as conn:
            record = await idempotency_repo.get(
                conn, user_id, key, settings.idempotency_stale_seconds
            )
            if record is None:
                # The owner declined or faulted and gave the key back.
                key_id = await try_claim_key(conn)
            elif record.fingerprint != request_fingerprint:
                raise ConflictError(ErrorCode.IDEMPOTENCY_KEY_REUSED)
            elif record.state is IdempotencyState.COMPLETED and record.response_body:
                return ReserveOutcome(body=record.response_body, replayed=True)
            elif record.stale:
                key_id = await idempotency_repo.reclaim_if_stale(
                    conn, record.id, settings.idempotency_stale_seconds
                )
            else:
                key_id = None
        if key_id is not None:
            return key_id
        if time.monotonic() >= deadline:
            raise _in_progress()
        await asyncio.sleep(settings.idempotency_poll_interval_ms / 1000)


def _in_progress() -> ConflictError:
    return ConflictError(
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        headers={Header.RETRY_AFTER: str(settings.idempotency_retry_after_seconds)},
    )


async def _claim(
    principal: Principal,
    show: Show,
    labels: list[str],
    hold_ttl_seconds: int | None,
    key_id: UUID,
) -> ReserveOutcome:
    reservation_id = uuid4()
    ttl_or_none = None if hold_ttl_seconds is None else min(hold_ttl_seconds, show.hold_ttl_seconds)
    try:
        # T2.
        async with transaction() as conn:
            if not await idempotency_repo.lock_owned(conn, key_id):
                # Taken over as stale by a duplicate. That request owns the outcome now.
                raise _in_progress()
            await seat_repo.lock_quota(conn, principal.user_id, show.id)
            held = await seat_repo.count_active_for_user(conn, show.id, principal.user_id)
            if held + len(labels) > show.per_user_limit:
                raise ConflictError(
                    ErrorCode.PER_USER_LIMIT,
                    details={"limit": show.per_user_limit, "currently_held": held},
                )
            claimed = await seat_repo.claim_many(
                conn,
                show_id=show.id,
                labels=labels,
                user_id=principal.user_id,
                reservation_id=reservation_id,
                ttl_or_none=ttl_or_none,
                show_price_paise=show.price_paise,
            )
            if len(claimed) < len(labels):
                # Raising rolls T2 back, which is what makes the claim all-or-nothing:
                # the seats that were claimed are released by the transaction itself.
                await _raise_shortfall(conn, show.id, labels, {seat.label for seat in claimed})
            superseded = await reservation_repo.close_superseded_claims(
                conn, [seat.id for seat in claimed]
            )
            reservation = await reservation_repo.create(
                conn,
                reservation_id=reservation_id,
                show_id=show.id,
                user_id=principal.user_id,
                seats=claimed,
                currency=show.currency,
                ttl_or_none=ttl_or_none,
                idempotency_key_id=key_id,
            )
            body = _body(reservation)
            # Inside T2: a third transaction would let a crash leave a committed
            # reservation whose key says in_progress forever.
            await idempotency_repo.complete(
                conn,
                key_id=key_id,
                status_code=201,
                response_body=body,
                reservation_id=reservation.id,
            )
    except BaseException:
        await asyncio.shield(_release_key(key_id))
        raise

    metrics.superseded_claims_closed_total.inc(superseded)
    if reservation.status is ReservationStatus.CONFIRMED:
        metrics.reservations_confirmed_total.inc()
    else:
        metrics.reservations_held_total.inc()
    logger.info(
        LogEvent.RESERVATION_CREATED,
        extra={
            "user_id": str(principal.user_id),
            "show_id": str(show.id),
            "reservation_id": str(reservation.id),
            "seat_labels": labels,
            "outcome": reservation.status.value,
        },
    )
    return ReserveOutcome(body=body, replayed=False)


async def _raise_shortfall(conn: Any, show_id: UUID, labels: list[str], claimed: set[str]) -> None:
    unknown = await seat_repo.labels_not_in_show(conn, show_id, labels)
    if unknown:
        raise NotFoundError(ErrorCode.SEAT_NOT_FOUND, details={"seats": unknown})
    raise ConflictError(
        ErrorCode.SEAT_TAKEN,
        details={
            "requested": labels,
            "conflicts": [label for label in labels if label not in claimed],
        },
    )


async def _release_key(key_id: UUID) -> None:
    try:
        async with acquire() as conn:
            await idempotency_repo.release(conn, key_id)
    except Exception as exc:  # noqa: BLE001 - must not mask the error being propagated
        # The key stays in_progress and the staleness reclaim recovers it.
        logger.error(LogEvent.IDEMPOTENCY_RELEASE_FAILED, exc_info=exc)


def _body(reservation: Reservation) -> dict[str, Any]:
    return ReservationResponse.of(reservation).model_dump(mode="json", exclude_none=True)


async def cancel(principal: Principal, reservation_id: UUID) -> Reservation:
    """Release a reservation its owner no longer wants: a live hold, or a confirmed
    booking. Locks its own reservation row, then seats in label order, then claim rows
    for seats already locked — the tiers of the deadlock argument."""
    async with transaction() as conn:
        decided = await reservation_repo.cancel_owned(conn, reservation_id, principal.user_id)
        if decided:
            released = await seat_repo.release_for_reservation(conn, reservation_id)
            await reservation_repo.close_claims_for_reservation(conn, reservation_id)
            reservation = await _owned(conn, reservation_id, principal.user_id)
            _require_every_seat(reservation, released)
    if not decided:
        return await _explain(principal, reservation_id, repeat_of=ReservationStatus.CANCELLED)
    metrics.reservations_cancelled_total.inc()
    _log_transition(LogEvent.RESERVATION_CANCELLED, reservation)
    return reservation


async def confirm(principal: Principal, reservation_id: UUID) -> Reservation:
    async with transaction() as conn:
        decided = await reservation_repo.confirm_owned(conn, reservation_id, principal.user_id)
        if decided:
            promoted = await seat_repo.confirm_for_reservation(conn, reservation_id)
            reservation = await _owned(conn, reservation_id, principal.user_id)
            _require_every_seat(reservation, promoted)
    if not decided:
        return await _explain(principal, reservation_id, repeat_of=ReservationStatus.CONFIRMED)
    metrics.reservations_confirmed_total.inc()
    _log_transition(LogEvent.RESERVATION_CONFIRMED, reservation)
    return reservation


async def get(principal: Principal, reservation_id: UUID) -> Reservation:
    async with acquire() as conn:
        return await _owned(conn, reservation_id, principal.user_id)


async def list_for_user(
    principal: Principal,
    *,
    show_id: UUID | None,
    status: ReservationStatus | None,
    cursor: str | None,
    limit: int,
) -> Page[Reservation]:
    after = parse_cursor(cursor)
    async with acquire() as conn:
        reservations = await reservation_repo.list_for_user(
            conn,
            principal.user_id,
            show_id=show_id,
            status=status.value if status else None,
            after=after,
            limit=limit + 1,
        )
    return page_of(reservations, limit, lambda r: (r.created_at, r.id))


async def _owned(conn: Any, reservation_id: UUID, user_id: UUID) -> Reservation:
    reservation = await reservation_repo.get_owned(conn, reservation_id, user_id)
    if reservation is None:
        # Also the answer for someone else's reservation: a 403 would confirm it exists.
        raise NotFoundError(ErrorCode.RESERVATION_NOT_FOUND)
    return reservation


def _require_every_seat(reservation: Reservation, affected: list[str]) -> None:
    """Believed unreachable — `now()` is transaction-stable, so a reservation that is
    unexpired cannot own an expired seat — but asserted, because the argument rests
    on that semantic. Raising rolls the transition back."""
    if affected != reservation.labels:
        raise ConflictError(ErrorCode.SEAT_TAKEN, details={"requested": reservation.labels})


_DECLINE_BY_STATUS = {
    ReservationStatus.CANCELLED: ErrorCode.RESERVATION_CANCELLED,
    ReservationStatus.EXPIRED: ErrorCode.RESERVATION_EXPIRED,
}


async def _explain(
    principal: Principal, reservation_id: UUID, *, repeat_of: ReservationStatus
) -> Reservation:
    """The guarded UPDATE matched nothing; say why. This read is diagnosis, not
    control: the decision is already made, and it only chooses the code (ADR-022)."""
    reservation = await get(principal, reservation_id)
    if reservation.status is repeat_of:
        return reservation
    raise ConflictError(
        _DECLINE_BY_STATUS.get(reservation.status, ErrorCode.SEAT_TAKEN),
        details={"status": reservation.status.value},
    )


def _log_transition(event: LogEvent, reservation: Reservation) -> None:
    logger.info(
        event,
        extra={
            "user_id": str(reservation.user_id),
            "show_id": str(reservation.show_id),
            "reservation_id": str(reservation.id),
            "seat_labels": reservation.labels,
            "outcome": reservation.status.value,
        },
    )
