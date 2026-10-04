# Auth and RBAC

Identity is derived from a verified token and from nothing else. A request body never influences who is acting.

## Principals

| Principal | `role` | `is_guest` | Credentials | May |
|---|---|---|---|---|
| Admin | `admin` | false | email + password | everything a user may, plus show management |
| User | `user` | false | email + password | reserve, confirm, cancel, read own |
| Guest | `user` | true | none — token only | identical to user, minus account management |

A guest is a real `users` row, not a special case in the request path. Every authorization check, limit, and ownership rule applies to a guest exactly as to a registered user, which means there is no guest-specific code path to get wrong.

## Tokens

JWT, HS256, signed with a secret from configuration. Access tokens are short-lived; refresh tokens are longer-lived and stateless.

```json
{
  "sub":  "<user_id>",
  "role": "user",
  "gst":  true,
  "typ":  "access",
  "jti":  "<uuid>",
  "iat":  1730000000,
  "exp":  1730000900,
  "iss":  "seat-reservation"
}
```

- `sub` is the only source of acting identity.
- `role` is carried in the token to avoid a user lookup per request. The trade-off: a role change does not take effect until the access token expires. Acceptable given short access lifetimes; recorded as RISK-002.
- `typ` distinguishes access from refresh. A refresh token presented as a bearer credential on a business route is rejected — without this check, the long-lived token becomes a long-lived access token.
- `jti` gives a log line per-token identity without logging the token.

Verification rejects, in this order: missing or malformed header, bad signature, wrong issuer, wrong `typ`, expired. Every failure is 401 `UNAUTHENTICATED` with no detail about which check failed.

**Guest token lifetime must exceed `MAX_HOLD_TTL_SECONDS`**, or a guest's token expires before they can confirm the longest hold the service will issue. Validated at startup, failing the boot rather than producing a confusing runtime failure (ADR-031). It is a *necessary* bound, not a guarantee: a token minted shortly before a maximum-length hold can still lapse first, which is RISK-006. Since ADR-017 a reserve confirms by default, so only a guest that deliberately opts into a hold can meet this at all, and the cost is one retry with no seat lost.

## Passwords

Argon2id via `argon2-cffi` with the library's default parameters. Hashing is CPU-bound and runs in a bounded thread pool so it cannot stall the event loop — unbounded, a login flood becomes a denial of service against the reserve path.

Policy: a configured minimum length (`PASSWORD_MIN_LENGTH`), a maximum of 128, no composition rules. Login compares in constant time and returns the same message and comparable latency whether or not the email exists, hashing a dummy value on the miss path to avoid a timing oracle.

## Flows

### Register — `POST /auth/register`

Insert a `user` row with role `user`. Email uniqueness is enforced by `uq_users_email`, not by a prior existence check — the check-then-insert race would allow two rows for one email. The unique violation is translated to 409 `EMAIL_TAKEN`.

### Login — `POST /auth/login`

Look up by lowercased email, verify the hash, issue access + refresh. Rate limited tightly per client address: this is the brute-force surface.

### Guest — `POST /auth/guest`

Insert a row with `is_guest=true`, `email=NULL`, `password_hash=NULL`, return a guest access token. No refresh token — a guest session is intentionally bounded; to persist, upgrade.

Rate limited per IP, since there is no principal yet to limit against. This is the one place an IP-based limit is unavoidable, and the ceiling is set so a legitimate on-sale rush of distinct buyers is not throttled.

### Upgrade — `POST /auth/upgrade`

Requires a valid guest token. One `UPDATE` sets `email`, `password_hash`, and `is_guest=false` on the same `user_id`:

```sql
UPDATE users SET email=$email, password_hash=$hash, is_guest=false, updated_at=now()
 WHERE id = $user_id AND is_guest = true
RETURNING id;
```

Guarded on `is_guest = true`, so two concurrent upgrades of one guest produce one winner. `ck_users_creds` makes a half-upgraded row unrepresentable. Because the `user_id` is unchanged, every reservation already held survives the upgrade with no data movement — which is the whole point of modelling guests as real users.

A non-guest token returns 409 `ALREADY_REGISTERED`; a taken email returns 409 `EMAIL_TAKEN` and the row stays a guest.

### Refresh — `POST /auth/refresh`

Accepts a `typ=refresh` token, issues a new access token. Stateless: no server-side session store, so logout is client-side token disposal. Revocation would need a `jti` denylist; deferred until a requirement asks for it (open question 2 in [01-requirements.md](01-requirements.md)).

## Authorization wiring

Every protected route declares its requirement through `Depends`. The handler body never inspects the request for identity.

```
get_current_user      read the Authorization header, verify the token (signature,
                      issuer, expiry, typ=access), build a Principal
require_admin         get_current_user, assert role == admin, else 403 FORBIDDEN
```

Exposed as the annotated types `CurrentUser` and `AdminUser` in `api/deps.py`. The principal is passed to the service as an argument; it is not bound to a context variable.

Routes with no principal dependency are public by construction, and the audience of every route is recorded in the table in [06-apis.md](06-apis.md). A route whose audience is not in that table is unreviewed.

## Permission matrix

| Route | Admin | User | Guest | Anonymous |
|---|---|---|---|---|
| `POST /auth/register` `/login` `/guest` | ✓ | ✓ | ✓ | ✓ |
| `POST /auth/upgrade` | — | — | ✓ | — |
| `POST /auth/refresh` | ✓ | ✓ | — | — |
| `GET /auth/me` | ✓ | ✓ | ✓ | — |
| `POST /shows` | ✓ | ✗ 403 | ✗ 403 | ✗ 401 |
| `GET /shows` `GET /shows/{id}` | ✓ | ✓ | ✓ | ✓ |
| `POST /shows/{id}/reserve` | ✓ | ✓ | ✓ | ✗ 401 |
| `POST /reservations/{id}/confirm` `/cancel` | owner only | owner only | owner only | ✗ 401 |
| `GET /reservations` `GET /reservations/{id}` | own only | own only | own only | ✗ 401 |
| `GET /healthz` `/readyz` `/metrics` | ✓ | ✓ | ✓ | ✓ |

An admin has no implicit ownership override. `POST /reservations/{id}/cancel` as an admin on another principal's hold is refused exactly as a user's would be; an override, if ever needed, is a separate explicitly-audited admin route.

## Ownership checks

Ownership is a `WHERE` clause, not a post-fetch comparison:

```sql
SELECT ... FROM reservations WHERE id = $id AND user_id = $principal_id
```

A fetch-then-compare is one forgotten `if` away from an authorization bypass, and it discloses existence through latency. With the predicate in the query, a non-owner is indistinguishable from a non-existent row.

A reservation the principal does not own returns **404 `RESERVATION_NOT_FOUND`**, not 403. A 403 confirms the resource exists, which lets an attacker enumerate reservation ids.

## Spoofing

A `user_id` in a request body is **ignored** — not validated against the token, not rejected, ignored. Pydantic request models for authenticated routes simply do not declare an identity field, so there is nothing to ignore at runtime and nothing a future edit can accidentally start trusting. A route that reads identity from a payload is a security defect, not a style issue.

Verified by a test that posts `{"seats":["A1"],"user_id":"<other>"}` and asserts the reservation belongs to the token's subject.

## Admin bootstrap

At startup, if a configured admin email is set and no admin exists, one is created from environment credentials and the fact is logged loudly. If an admin already exists, nothing happens — the bootstrap is idempotent and cannot overwrite a changed password. In an environment where the bootstrap credentials are absent, startup proceeds without an admin and logs a warning; admin routes then simply have no principal who can reach them.

## Threat notes

| Threat | Mitigation |
|---|---|
| Credential stuffing | A tight per-address rate limit on login; uniform failure response and latency. A per-email limit is future scope |
| Timing oracle on email existence | Dummy hash computed on the miss path |
| Token replay after role change | Short access lifetime; RISK-002 |
| Refresh token used as access token | `typ` claim checked on every business route |
| Guest flooding, and seat hoarding through minted guests | A per-address limit on `/auth/guest`. This bounds it and does not close it: each guest carries its own per-user seat limit (RISK-014) |
| Secret in an image or log | Secret only from the environment; never logged, never in a response, never in a repository |
| Algorithm confusion | Decoder pinned to a single algorithm; `none` and asymmetric variants rejected |
| Privilege escalation via body | No identity or role field exists on any authenticated request model |
| Hold hijack after expiry | Confirm is predicated on the owner, `status='held'` **and `hold_expires_at > now()`** (ADR-022), so a lapsed hold cannot be confirmed by its former holder whether or not its seat has been re-claimed. The earlier form relied on `reservation_id` having moved on, which is true only for a seat someone else actually took — a merely lapsed hold would still have matched |
