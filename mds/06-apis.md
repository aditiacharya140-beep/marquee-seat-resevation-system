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
| `GET` | `/shows` | public | yes | `read` | `shows.py` |
| `GET` | `/shows/{show_id}` | public | yes | `read` | `shows.py` |
| `DELETE` | `/shows/{show_id}` | admin | yes | `read` | `shows.py` |
| `POST` | `/shows/{show_id}/reserve` | any principal | **key required** | `reserve` | `reservations.py` |
| `POST` | `/reservations/{id}/confirm` | owner | yes | `reserve` | `reservations.py` |
| `POST` | `/reservations/{id}/cancel` | owner | yes | `reserve` | `reservations.py` |
| `GET` | `/reservations` | any principal | yes | `read` | `reservations.py` |
| `GET` | `/reservations/{id}` | owner | yes | `read` | `reservations.py` |
| `GET` | `/healthz` | public | yes | exempt | `health.py` |
| `GET` | `/readyz` | public | yes | exempt | `health.py` |
| `GET` | `/metrics` | public | yes | exempt | `metrics.py` |
| `GET` | `/admin/overview`, `/admin/audit`, `/admin/logs`, `/admin/shows` | admin | yes | `read` | `admin.py` |
| `GET` | `/`, `/admin`, `/static/*` | public | yes | exempt | `pages.py` — the booking page and the admin console |
| `GET` | `/` | public | yes | exempt | `pages.py` |
| `GET` | `/static/{file}` | public | yes | exempt | mounted in `main.py` |

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

Only `name`, `seats`, `price_paise` are required; the rest default from config — `event_kind` from `DEFAULT_EVENT_KIND` and validated against `ALLOWED_EVENT_KINDS`, `currency` from `DEFAULT_CURRENCY`, `per_user_limit` and `hold_ttl_seconds` from their own settings. `seat_overrides` is how tiered pricing and layout metadata arrive without a second request.

This is the one endpoint that **rejects** unknown body fields; see the conventions at the end of this document.

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

`422 VALIDATION_ERROR` — empty `seats`, duplicate labels, negative or non-integer `price_paise`, label over the configured length, seat count over the configured maximum, an override naming a label not in `seats`, an `event_kind` outside `ALLOWED_EVENT_KINDS`, or **any unknown field**.
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

A seat whose hold has lapsed reports `available`, matching what a claim would see — status is derived on read from the single expression in `db/sql.py`, not read from the stored column, because nothing rewrites the stored column (ADR-017). `held_by` is **not** exposed — seat ownership is not public information. `held_until` is exposed because a waiting buyer benefits from knowing when a seat frees up.

A `held` seat arises only from a reserve that opted into a hold; a default reserve goes straight to `confirmed`. All three states are reachable, which is what makes this contract honoured by behaviour rather than by a status nothing produces.

`404 SHOW_NOT_FOUND`

### `DELETE /shows/{show_id}` — admin

**200** `{ "show_id": "…", "deleted": { "reservations": 2, "seats": 3 } }`

Removes the show, its seats, every reservation on it and the idempotency keys of those reservations, in one transaction. **There is no undo and no refund step.** A reserve in flight either completes before the delete or answers 404 `SHOW_NOT_FOUND`; none is left half-applied (ADR-043).

`404 SHOW_NOT_FOUND` · `403 FORBIDDEN` · `401 UNAUTHENTICATED`

### `GET /shows`

Query: `limit`, `cursor`, `status`, `event_kind`. **200**
```json
{ "items": [ { "show_id": "…", "name": "…", "event_kind": "cinema", "status": "on_sale",
               "price_paise": 25000, "currency": "INR", "total_seats": 200,
               "created_at": "2026-10-03T12:00:00Z" } ],
  "next_cursor": "…|null" }
```
Newest first, keyset-paginated on the immutable `(created_at, id)`, so paging through a catalogue that is being booked neither skips nor repeats a show. `limit` is bounded by `PAGE_SIZE_MAX`. **No availability counts and no seat detail** — a list of shows must not scan every seat of every show; `GET /shows/{id}` is the exact source. A malformed cursor is 422.

---

## Reserve

### `POST /shows/{show_id}/reserve`

Authenticated. Idempotency key **required**, from the `Idempotency-Key` header or the body. If both are present and differ → 422.

```json
{ "seats": ["A12", "A13"], "idempotency_key": "d4f1…" }
```

**`hold_ttl_seconds` is optional and selects between the two outcomes** (ADR-017). Omitted — the default and primary path — the seats are confirmed outright. Present, it is clamped to the show's configured maximum and the seats are held until `expires_at`.

**201 — no `hold_ttl_seconds`: confirmed**
```json
{
  "reservation_id": "…", "show_id": "…", "user_id": "…",
  "seats": ["A12", "A13"],
  "amount_paise": 65000, "currency": "INR",
  "status": "confirmed",
  "confirmed_at": "2026-10-03T12:00:00Z",
  "created_at": "2026-10-03T12:00:00Z"
}
```

**201 — with `hold_ttl_seconds`: held**
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

`expires_at` is present only on a held reservation and reports the TTL **actually applied** after clamping, which may be lower than the one requested. `confirmed_at` is present only on a confirmed one.

`user_id` is the token's subject. An identity field in the body has no effect — and cannot, because no request model declares one (ADR-028). A reserve carrying `"user_id": "<someone else>"` therefore returns **201 owned by the token subject**, not 422.

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
| 409 | `IDEMPOTENCY_KEY_REUSED` | same key, different canonical request — including the **same body against a different show** |
| 409 | `IDEMPOTENCY_IN_PROGRESS` | concurrent duplicate still running past the wait bound; `Retry-After` |
| 409 | `SHOW_NOT_ON_SALE` | show is `draft` or `closed`, or outside its sale window |
| 404 | `SHOW_NOT_FOUND` / `SEAT_NOT_FOUND` | unknown show, or a label not in this show |
| 422 | `VALIDATION_ERROR` | no key, key too long, empty or duplicated labels, more labels than the limit |
| 429 | `RATE_LIMITED` | this principal's ceiling exceeded; `Retry-After`, `X-RateLimit-*`, `details.route_class` |
| 401 | `UNAUTHENTICATED` | missing or invalid token |

### Idempotency behaviour a client must code against

- **A replay always answers 200**, never 201, with the stored body and `Idempotent-Replay: true` (ADR-029). Read the header, not the status, to tell "created" from "already created".
- **Only successes are stored.** A request whose outcome was a decline releases the key, so retrying with the same key is a genuine new attempt that may succeed — the seat may have freed (ADR-020). Declines are not replayed.
- **A key is bound to one show.** The fingerprint covers the operation, the show id from the path, the sorted de-duplicated labels, and `hold_ttl_seconds` when present. Reusing a key across shows is 409 `IDEMPOTENCY_KEY_REUSED` (ADR-021).

Never 5xx. Lock timeouts, serialization failures, deadlocks and pool pressure on this path are translated to 409 or retried within the request. A `statement_timeout` is the exception and is deliberately not reachable here: it is configured above `lock_timeout`, so the lock timeout always fires first and a 503 on this path means a genuine fault (ADR-027).

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

Only a `held` reservation is confirmable through this route; a reserve with no `hold_ttl_seconds` arrives already confirmed and needs no call here.

`409 RESERVATION_EXPIRED` — the hold lapsed; `details.status`. Returned whether or not the seats have since been re-claimed: the statement carries `hold_expires_at > now()`, so a lapsed hold is never promotable (ADR-022)
`409 RESERVATION_CANCELLED`
`409 SEAT_TAKEN` — a seat the reservation held is no longer its own; the confirm is predicated on current ownership, so it cannot steal the seat back
`404 RESERVATION_NOT_FOUND` — unknown, or owned by another principal

### `POST /reservations/{id}/cancel` — owner only

**200** — the reservation, as below, with `"status": "cancelled"` and `cancelled_at`.

Idempotent. Works on a **confirmed** reservation and on a live hold (ADR-040). Releases the seats to `available` and closes their claim rows; the seats are immediately re-bookable, and the owner's per-user allowance is freed. A repeat cancel is 200 and changes nothing, even if someone else has since booked the seats.

`409 RESERVATION_EXPIRED` — the hold already lapsed, so its seats are already effectively available and there is nothing to release; reporting the real state beats a successful no-op
`404 RESERVATION_NOT_FOUND` — including when owned by another principal, so reservation ids cannot be enumerated

### `GET /reservations`

Query: `show_id`, `status`, `limit`, `cursor`. Returns only the principal's own reservations, newest first, keyset-paginated: `{ "items": [ … ], "next_cursor": "…|null" }`. `status` filters on the **effective** status, so a lapsed hold is found under `expired`.

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
{ "status": "ready", "rate_limit_enabled": true,
  "checks": { "database": { "ok": true, "latency_ms": 3 } } }
```
**503**
```json
{ "status": "not_ready", "rate_limit_enabled": true,
  "checks": { "database": { "ok": false, "error": "ConnectionRefusedError" } } }
```
Executes a real query. Fails closed, never from a cached result.

### `GET /metrics`

Prometheus text format. Catalogue in [10-observability.md](10-observability.md).

### `GET /` and `GET /static/{file}`

The web page and its two assets, served from `app/static/` as they are ([18-frontend.md](18-frontend.md)). A missing asset answers `404 ROUTE_NOT_FOUND` in the envelope like any other unmatched path.

### Admin — `GET /admin/*`

All four require the admin role: 401 without a token, 403 for a user or guest. Every query is bounded by a time window (`window_minutes`, at most `ADMIN_MAX_WINDOW_MINUTES`) and a row limit (`limit`, at most `ADMIN_MAX_ROWS`).

| Path | Returns |
|---|---|
| `/admin/overview?window_minutes=15` | `requests` over the window — `total`, `by_status_class`, `by_outcome`, `by_route` with `p50_ms`/`p95_ms`, `per_minute` — plus `counters` (this process's own metrics) and `system` (version, uptime, pool in use, audit buffer, whether rate limiting is on) |
| `/admin/audit?status_code=&outcome_code=&request_id=&user_id=&show_id=&before_id=` | `items`, newest first, and `next_before_id` for the next page |
| `/admin/logs?level=&event=&request_id=` | `items`: this process's most recent log lines, newest first |
| `/admin/shows` | recent shows with `available` and `total_seats` |

The console's own requests under `/admin` are not themselves audited, or reading the trail would fill it.

---

## Conventions

| Concern | Rule |
|---|---|
| Request id | `X-Request-ID` accepted when a valid UUID, otherwise minted; echoed on every response including errors |
| Idempotency key | `Idempotency-Key` header preferred; body accepted; conflict between the two is 422 |
| Replay marker | `Idempotent-Replay: true` on any replayed response |
| Pagination | Keyset via opaque `cursor`; `limit` bounded by config; offset pagination is not offered |
| Unknown body fields | **Admin endpoints reject with 422; every other endpoint ignores** (ADR-028). See below |
| Seat label order | Response `seats` arrays are sorted for stable comparison |
| Money | Integer paise everywhere; no decimal string, no float, in any direction |
| Time | RFC 3339 with `Z`; all server-generated from the database clock |
| Trailing slashes | Not redirected; the canonical path is the one in the route table |
| Errors | One envelope, always, including 422, 429, and framework-level 404 and 405 |

### Unknown body fields

`POST /shows` — the only admin write — rejects unknown fields with 422. Its body becomes durable configuration: prices, per-user limit, hold TTL, per-seat overrides. A mistyped field name there silently produces a show that sells the wrong seats at the wrong price, discovered by customers, so failing loudly is far cheaper.

Every other endpoint ignores unknown fields. Their effect is determined entirely by the path, the token subject and the named fields, so a stray field cannot change the outcome — and rejecting it would convert a harmless client quirk into a failed booking during exactly the on-sale the service exists for.

Identity safety is **independent of this policy**: no request model anywhere declares an identity field, so there is nothing for a body value to bind to. A reserve carrying another principal's id answers 201 owned by the token subject, which is what REQ-005 requires and what a 422 would not demonstrate.

### Framework-level failures

A request that matches no route, or matches a path with an unsupported method, answers in the same envelope as everything else (ADR-023):

| Status | Code | When |
|---|---|---|
| 404 | `ROUTE_NOT_FOUND` | no route matches the path. Includes a trailing-slash variant, since slashes are not redirected |
| 405 | `METHOD_NOT_ALLOWED` | the path exists for other methods. The `Allow` header is preserved |

Both are declines, logged at `info`. They are registered in the error-code registry like every other code; the full set of codes that appear in no endpoint table is listed in [08-error-logging.md](08-error-logging.md).
