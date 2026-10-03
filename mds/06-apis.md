# APIs

All bodies are JSON. All money is integer paise. All ids are UUID strings. All timestamps are RFC 3339 UTC.

Every response, success or failure, carries `X-Request-ID`. Every failure uses the envelope in [08-error-logging.md](08-error-logging.md).

## Route table

| Method | Path | Audience | Idempotent | Rate class | Route module |
|---|---|---|---|---|---|
| `POST` | `/auth/register` | public | no | `auth` | `auth.py` |
| `POST` | `/auth/login` | public | no | `auth` | `auth.py` |
| `POST` | `/auth/guest` | public | no | `guest` (per IP) | `auth.py` |
| `POST` | `/auth/upgrade` | guest | no | `auth` | `auth.py` |
| `POST` | `/auth/refresh` | user, admin | no | `auth` | `auth.py` |
| `GET` | `/auth/me` | any principal | yes | `read` | `auth.py` |
| `POST` | `/shows` | admin | key optional | `admin` | `shows.py` |
| `GET` | `/shows` | public | yes | `read` | *deferred, ADR-015* |
| `GET` | `/shows/{show_id}` | public | yes | `read` | `shows.py` |
| `POST` | `/shows/{show_id}/reserve` | any principal | **key required** | `reserve` | `reservations.py` |
| `POST` | `/reservations/{id}/confirm` | owner | yes | `reserve` | `reservations.py` |
| `POST` | `/reservations/{id}/cancel` | owner | yes | `reserve` | `reservations.py` |
| `GET` | `/reservations` | any principal | yes | `read` | `reservations.py` |
| `GET` | `/reservations/{id}` | owner | yes | `read` | `reservations.py` |
| `GET` | `/healthz` | public | yes | exempt | `health.py` |
| `GET` | `/readyz` | public | yes | exempt | `health.py` |
| `GET` | `/metrics` | public | yes | exempt | `metrics.py` |

Routes are grouped by resource and audience. No helper logic lives in any of these modules — a handler resolves dependencies, calls one service method, and returns a response model.

---

## Auth

### `POST /auth/register`

```json
{ "email": "a@example.com", "password": "………" }
```

**201**
```json
{
  "user_id": "…", "email": "a@example.com", "role": "user", "is_guest": false,
  "access_token": "…", "refresh_token": "…", "token_type": "bearer", "expires_in": 900
}
```

`409 EMAIL_TAKEN` · `422 VALIDATION_ERROR` (malformed email, password below policy)

### `POST /auth/login`

```json
{ "email": "a@example.com", "password": "………" }
```

**200** — same body as register.
`401 INVALID_CREDENTIALS` — identical message and comparable latency whether or not the email exists.

### `POST /auth/guest`

No body. **201**
```json
{ "user_id": "…", "role": "user", "is_guest": true,
  "access_token": "…", "token_type": "bearer", "expires_in": 3600 }
```
No refresh token. `429 RATE_LIMITED` on per-IP abuse.

### `POST /auth/upgrade`

Guest token required.
```json
{ "email": "a@example.com", "password": "………" }
```
**200** — same shape as register, same `user_id`, `is_guest: false`. Reservations already held remain attached.
`409 ALREADY_REGISTERED` · `409 EMAIL_TAKEN` · `401 UNAUTHENTICATED`

### `POST /auth/refresh`

```json
{ "refresh_token": "…" }
```
**200** `{ "access_token": "…", "token_type": "bearer", "expires_in": 900 }`
`401 UNAUTHENTICATED` — expired, wrong `typ`, or bad signature.

### `GET /auth/me`

**200** `{ "user_id": "…", "email": "…|null", "role": "user", "is_guest": false }`

---

## Shows

### `POST /shows` — admin

```json
{
  "name": "friday-night",
  "seats": ["A1", "A2", "A12"],
  "price_paise": 25000,
  "event_kind": "cinema",
  "per_user_limit": 4,
  "hold_ttl_seconds": 120,
  "currency": "INR",
  "seat_overrides": { "A12": { "price_paise": 40000, "section": "premium" } }
}
```

Only `name`, `seats`, `price_paise` are required; the rest default from config. `seat_overrides` is how tiered pricing and layout metadata arrive without a second request.

**201**
```json
{
  "show_id": "…", "name": "friday-night", "event_kind": "cinema",
  "price_paise": 25000, "currency": "INR",
  "per_user_limit": 4, "hold_ttl_seconds": 120,
  "status": "on_sale", "total_seats": 3,
  "counts": { "available": 3, "held": 0, "confirmed": 0, "total": 3 },
  "seats": [
    { "label": "A1",  "status": "available", "price_paise": 25000 },
    { "label": "A2",  "status": "available", "price_paise": 25000 },
    { "label": "A12", "status": "available", "price_paise": 40000, "section": "premium" }
  ],
  "created_at": "2026-10-03T12:00:00Z"
}
```

`422 VALIDATION_ERROR` — empty `seats`, duplicate labels, negative or non-integer `price_paise`, label over the configured length, seat count over the configured maximum, an override naming a label not in `seats`.
`403 FORBIDDEN` — non-admin.

Show and all seats are created in one transaction. A validation failure creates nothing.

### `GET /shows/{show_id}`

**200**
```json
{
  "show_id": "…", "name": "friday-night", "status": "on_sale",
  "price_paise": 25000, "currency": "INR",
  "per_user_limit": 4, "hold_ttl_seconds": 120, "total_seats": 3,
  "counts": { "available": 1, "held": 1, "confirmed": 1, "total": 3 },
  "seats": [
    { "label": "A1",  "status": "available", "price_paise": 25000 },
    { "label": "A2",  "status": "held",      "price_paise": 25000, "held_until": "2026-10-03T12:02:00Z" },
    { "label": "A12", "status": "confirmed", "price_paise": 40000 }
  ]
}
```

`available + held + confirmed == total_seats` always. Counts and seat rows come from one snapshot, so they can never disagree with each other.

A seat whose hold has lapsed but has not been swept reports `available`, matching what a claim would see. `held_by` is **not** exposed — seat ownership is not public information. `held_until` is exposed because a waiting buyer benefits from knowing when a seat frees up.

`404 SHOW_NOT_FOUND`

### `GET /shows` — deferred (ADR-015)

Not built. Nothing depends on a paginated catalogue, and deferring it keeps the cursor codec off the critical path. Shape when it lands:


Query: `limit`, `cursor`, `status`, `event_kind`. **200**
```json
{ "items": [ { "show_id": "…", "name": "…", "status": "on_sale",
               "counts": { "available": 120, "held": 4, "confirmed": 76, "total": 200 } } ],
  "next_cursor": "…|null" }
```
Keyset pagination on `(created_at, id)`. `limit` is bounded by config. Seat detail is omitted deliberately — a list of shows must not scan every seat of every show.

---

## Reserve

### `POST /shows/{show_id}/reserve`

Authenticated. Idempotency key **required**, from the `Idempotency-Key` header or the body. If both are present and differ → 422.

```json
{ "seats": ["A12", "A13"], "idempotency_key": "d4f1…", "hold_ttl_seconds": 120 }
```

`hold_ttl_seconds` is optional and clamped to the show's configured maximum.

**201**
```json
{
  "reservation_id": "…", "show_id": "…", "user_id": "…",
  "seats": ["A12", "A13"],
  "amount_paise": 65000, "currency": "INR",
  "status": "held",
  "expires_at": "2026-10-03T12:02:00Z",
  "created_at": "2026-10-03T12:00:00Z"
}
```

`user_id` is the token's subject. An identity field in the body has no effect.

**Semantics: all-or-nothing.** If any requested seat is unavailable, nothing is claimed:

```json
{ "error": { "code": "SEAT_TAKEN", "message": "One or more seats are no longer available",
             "details": { "requested": ["A12","A13"], "conflicts": ["A13"] },
             "request_id": "…" } }
```

| Status | Code | When |
|---|---|---|
| 201 | — | every requested seat claimed |
| 200 | — | idempotent replay of a prior success, `Idempotent-Replay: true` |
| 409 | `SEAT_TAKEN` | any requested seat active for another principal, or a lock timeout |
| 409 | `PER_USER_LIMIT` | would exceed the show's limit; `details.limit`, `details.currently_held` |
| 409 | `IDEMPOTENCY_KEY_REUSED` | same key, different canonical body |
| 409 | `IDEMPOTENCY_IN_PROGRESS` | concurrent duplicate still running past the wait bound; `Retry-After` |
| 409 | `SHOW_NOT_ON_SALE` | show is `draft` or `closed`, or outside its sale window |
| 404 | `SHOW_NOT_FOUND` / `SEAT_NOT_FOUND` | unknown show, or a label not in this show |
| 422 | `VALIDATION_ERROR` | no key, key too long, empty or duplicated labels, more labels than the limit |
| 429 | `RATE_LIMITED` | ceiling exceeded; `Retry-After` |
| 401 | `UNAUTHENTICATED` | missing or invalid token |

A replay returns the **original status code**, including an original decline — a 409 replayed as a 409. That is what exactly-once means for a request whose outcome was a decline.

Never 5xx. Lock timeouts, serialization failures, and pool pressure on this path are translated to 409 or retried within the request.

---

## Lifecycle

### `POST /reservations/{id}/confirm` — owner only

**200**
```json
{ "reservation_id": "…", "show_id": "…", "user_id": "…", "seats": ["A12","A13"],
  "amount_paise": 65000, "currency": "INR", "status": "confirmed",
  "confirmed_at": "2026-10-03T12:01:30Z" }
```

Idempotent: confirming an already-confirmed reservation returns 200 with the same body.

`409 RESERVATION_EXPIRED` — the hold lapsed; `details.status`
`409 RESERVATION_CANCELLED`
`409 SEAT_TAKEN` — the hold lapsed and a seat was re-claimed in the interim; the confirm is predicated on current ownership, so it cannot steal the seat back
`404 RESERVATION_NOT_FOUND` — unknown, or owned by another principal

### `POST /reservations/{id}/cancel` — owner only

**200** `{ "reservation_id": "…", "status": "cancelled", "seats": ["A12","A13"], "cancelled_at": "…" }`

Idempotent. Releases held seats to `available`; they are immediately re-bookable.

`409 RESERVATION_CONFIRMED` — a confirmed reservation is not cancellable through this route
`404 RESERVATION_NOT_FOUND` — including when owned by another principal, so reservation ids cannot be enumerated

### `GET /reservations`

Query: `show_id`, `status`, `limit`, `cursor`. Returns only the principal's own reservations, keyset-paginated.

### `GET /reservations/{id}`

**200** the reservation with its seats. `404` if not owned.

---

## Operations

### `GET /healthz`

**200** `{ "status": "ok", "service": "seat-reservation", "version": "…" }`
Touches no dependency. Always 200 while the process is alive.

### `GET /readyz`

**200**
```json
{ "status": "ready", "checks": { "database": { "ok": true, "latency_ms": 3 } } }
```
**503**
```json
{ "status": "not_ready", "checks": { "database": { "ok": false, "error": "connection refused" } } }
```
Executes a real query. Fails closed, never from a cached result.

### `GET /metrics`

Prometheus text format. Catalogue in [10-observability.md](10-observability.md).

---

## Conventions

| Concern | Rule |
|---|---|
| Request id | `X-Request-ID` accepted when a valid UUID, otherwise minted; echoed on every response including errors |
| Idempotency key | `Idempotency-Key` header preferred; body accepted; conflict between the two is 422 |
| Replay marker | `Idempotent-Replay: true` on any replayed response |
| Pagination | Keyset via opaque `cursor`; `limit` bounded by config; offset pagination is not offered |
| Unknown body fields | Rejected with 422 — a silently ignored field hides client bugs, and `user_id` must never be silently accepted |
| Seat label order | Response `seats` arrays are sorted for stable comparison |
| Money | Integer paise everywhere; no decimal string, no float, in any direction |
| Time | RFC 3339 with `Z`; all server-generated from the database clock |
| Trailing slashes | Not redirected; the canonical path is the one in the route table |
| Errors | One envelope, always, including 422 and 429 |
