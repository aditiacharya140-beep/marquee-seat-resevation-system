# Decision highlights

A curated record of the judgment calls that shaped this service, and of the defects caught before they shipped — with **provenance**: who drove each one.

This is distinct from [99-ledger.md](99-ledger.md). The ledger is the formal, append-only `ADR` / `LEARN` / `RISK` log: what was decided and why. This document records *how* the decision came about, what was nearly shipped instead, and which defects were caught at which stage. It exists because a design is only as trustworthy as the process that produced it, and because the honest provenance answers the write-up's AI-usage question directly rather than being reconstructed from memory afterwards.

**Maintenance rule.** Append an entry when a decision changes the design, a defect is caught before it ships, or an assumption is replaced by evidence. Record who drove it. Do not append routine implementation work — this is the signal, not the log.

---

## The decisions that mattered most

### Probing the atomic mechanism before writing any code

The entire design rests on PostgreSQL re-evaluating a `WHERE` clause after a blocking row lock is released. That was asserted in a document, and assertion is not evidence. Before a line of application code existed, the guarded `UPDATE` was probed directly against PostgreSQL 16.15: blocking re-evaluation, 50-way contention, winner-rollback, and lapsed-hold reclaim.

Result: 1 winner, 49 declined, **0 errors** — then reproduced through asyncpg rather than `psql`, confirming it holds through the real driver.

Why it mattered: the zero-errors figure is load-bearing. It means a loser is a *decision* (zero rows affected), not an exception to catch and translate. "No 5xx for a domain outcome" is therefore achievable rather than aspirational — and that was established on day one instead of discovered under load. ADR-001, LEARN-002.

### Deriving the per-user count instead of storing it

A stored counter must be decremented on cancel, on expiry, and on every future release path. Each is a chance to drift, and a drifted counter either blocks a legitimate booking forever or silently raises the limit. The count is derived from `seats` instead, with a quota row locked purely to serialise one principal's concurrent attempts.

Why it mattered beyond its own correctness: it set a precedent that was later load-bearing. When the catalogue list endpoint needed per-show availability counts, the tempting fix was a denormalised tally on `shows` — and rejecting it was automatic, because accepting it there while rejecting it for the limit would have been incoherent. One principle, applied twice. ADR-005.

### Rate limiting keyed on principal, not IP

Twenty thousand buyers behind one load generator share an IP but are distinct principals. An IP-keyed limiter on the reserve path would throttle precisely the burst the service exists to survive — turning a correct service into a wall of 429s.

This was caught by asking, before building the limiter, what it would actually *do* during the event it was built for. ADR-007.

### Postgres everywhere, no SQLite locally

Neither Postgres nor Docker was installed on the development machine, which made a local SQLite shortcut genuinely attractive. Rejected: SQLite cannot express `FOR UPDATE`, has different predicate re-evaluation semantics, and handles partial unique indexes differently. **The one thing that must be proven correct is exactly the part that differs between the engines**, so the shortcut would have left it untested until production. ADR-011.

### Audit that can never fail a booking

Audit writes go through a bounded in-memory queue drained in batches, with `put_nowait` on the request path. Awaiting queue capacity would make the audit trail a source of backpressure on bookings — inverting the priority. Losing an audit row is an inconvenience; failing a booking is a defect. Drops are counted and logged, never silent. ADR-008.

---

## Decisions the user drove

Recorded honestly, because several materially improved the design and two prevented me shipping something worse.

| Direction given | What it produced |
|---|---|
| Build a role-bounded agent team with self-enhancing skills | The roster, the stage gates, and the rule that a durable learning is written back into the skill rather than only the ledger — so each session starts smarter than the last |
| *"Grill me"* | Seven foundational decisions settled by interrogation before any building, rather than defaulted silently: hold model, partial-request semantics, guest identity, deploy target, rate-limit policy, audit write path, idempotency race behaviour |
| *"Never over-engineer"* | Three pieces cut that no requirement drove: the `events` table, `seats.row_label`/`seat_number`, and a deferred list endpoint. ADR-013/014/015 |
| *"We should not under-deliver"* | **Prevented** cutting four things an external review wanted gone — refresh tokens, guest upgrade, sale windows, tiered pricing. The review had optimised for the specification and missed the project's own requirements |
| *"Reason with me and then perform"* | Stopped a delegation mid-flight. The reasoning that followed produced a **better** answer than the one already recommended: holds with lazy expiry and **no sweeper worker**, because the sweeper's only job was making stored state match reality and effective status is derived on read anyway |
| *"Should we keep holds as well?"* | Turned a binary into the right design: `confirmed` as the default path, holds available opt-in. Keeps all three seat states genuinely reachable while removing the second-winner risk from the path that load actually exercises. ADR-017 |
| *"This is alarming"*, on reading that guests made the seat limit bypassable | Rate limiting, cut for the deadline, was built the same hour — and the write-up says plainly that it narrows the loophole rather than closing it |
| *"Should we remove guests completely?"* | Kept, after the reasoning: registration is exactly as free as a guest, so removing guests deletes a tested feature without closing anything |
| Pasting the original brief back in and asking what was under-delivered | Found three things the AI had not flagged: cancel refused a confirmed booking, rate limiting would have turned a reviewer's burst into 429s, and reviewers had no admin sign-in. All three were the AI following its own documents past the brief (ADR-040, ADR-041, ADR-044) |
| *"I don't see a cancel option"*, from using the page | The button had not followed the service change. Found by a person using it, not by a test |
| *"Sync the mds with our codebase"* | The design set now describes what is built, with one document for what is not (ADR-035) |
| *"Reason with me"* on the list endpoint | Surfaced that a performance guarantee in the repository contract — *"never scans seats, reads precomputed counts"* — was unsupported by the schema. There were no such counts, and the only way to create them was the denormalised tally already rejected. Would have shipped as an inconsistency |
| *"This is broken right? I lost my booked ticket the moment my token refreshes"*, on the web page | Correct, and found by using the page rather than by any test: a guest's ticket was reachable only through a one-hour token held in the tab. The page now requires an account at the moment of booking. The user's first suggestion, a mobile number, was reasoned out of: without an OTP it would let anyone who knows a number read its tickets. ADR-037 |

The pattern worth naming: **every instance of being asked to reason before acting produced a better design than acting would have.** Twice it corrected a recommendation already on the table.

---

## Defects caught before shipping, by source

### By external review of the design

| Defect | Why it mattered |
|---|---|
| Lazy expiry collides with the backstop index | A lapsed hold keeps an active `reservation_seats` row, so a new claim violates `uq_seat_active_claim` and the **legitimate winner** gets a 409. The backstop would have fired on a correct operation. Critical once the sweeper was dropped, because lazy expiry became the *only* expiry mechanism |
| Cancel and confirm lock in scan order | The deadlock-freedom proof claimed ordered locking; the specified SQL was a plain `UPDATE ... WHERE reservation_id = $1`. The proof did not cover the statement it was proving |
| Stored declines have nowhere to be written | Two paragraphs six lines apart contradicted each other: one said a rolled-back transaction leaves the key `in_progress`, the other promised declines replay as declines |
| Idempotency scope absent from the unique constraint | Same key and same seats on a *different show* would replay the wrong show's reservation |
| Idempotency waiters hold a pool connection while polling | Would exhaust the pool under burst and produce exactly the 503s another decision forbids |
| Replay should return 200, not the original 201 | A retry would otherwise be counted as a second win on a hot seat — breaking a property that is directly measured |

### By the requirements decomposition, before any code existed

A requirement was **unsatisfiable**, not merely strict: REQ-048 demanded zero 5xx, while the scalability document named 503 on pool-acquire timeout as the one legitimate 5xx. A correct implementation could fail its own requirement. Resolved by permitting 503 only on genuine unavailability and requiring zero occurrences during a burst — which makes pool sizing *part of the requirement* rather than a tuning detail.

The same pass found that `unhandled_exceptions_total` — the direct measurement of that requirement — was required by no requirement at all. ADR-016.

### By the implementer, while verifying its own work

| Defect | Nature |
|---|---|
| Secrets printed on boot failure | Pydantic renders `input_value`, so a missing-variable error emitted the real `ADMIN_PASSWORD` to stdout. Found by actually running the failure case rather than assuming it was clean |
| Non-JSON log lines | Starlette's `ServerErrorMiddleware` re-raises unconditionally, so uvicorn logged every traceback a second time in plain text, breaking the single-line-JSON contract |
| Unmatched routes bypass the error envelope | `GET /nope` returns Starlette's default shape, contradicting "one envelope for every failure" — and no error code existed for it. Flagged rather than invented, which was the right call |
| A ticket's acceptance check was unsatisfiable | It required the error registry to equal the API contract's codes, but three operational codes are mandated elsewhere and appear in no contract table |
| `BaseHTTPMiddleware` would have broken the access log | It runs the application in a separate task, so an outcome code set inside the app is invisible to the middleware wrapping it. Pure ASGI middleware instead — an improvement on the specification, not a deviation from it |

### By running the built image, in the final build

| Defect | Nature |
|---|---|
| Migrations ran locally and failed in the container | `python -m alembic` puts the working directory on `sys.path`; the `alembic` console script the entrypoint uses does not. Every local run and every test passed. Found by `docker compose up`, minutes after "the migration works" (LEARN-013) |
| Compose and CI could not boot | Their `JWT_SECRET` values were shorter than the minimum the settings validate. Found by reading them against the config, before either was run (RISK-013) |

### By a test written for a path nobody expected to be wrong

Stale-key takeover let both the "dead" owner and its replacement proceed if the owner was merely slow — two owners of one idempotency key, and the loser's cleanup deleting the winner's key. Found while adding the missing test for that branch, not by review. Fixed by making the key's id the ownership token: a takeover rotates it, and the claim locks the key row by id before it touches a seat (ADR-033, LEARN-016).

### In the test harness, not the service

A concurrency probe hung indefinitely. The cause was entirely in the harness: fifty tasks each acquired a pooled connection *and then* waited on a fifty-wide barrier, against a pool of thirty. Thirty held connections waiting for twenty that could never obtain one.

Why it earned a permanent rule: barrier synchronisation is exactly how the concurrency suite forces genuine overlap, so the suite will meet this trap. Acquire every scarce resource before the barrier, size the pool above the participant count, and give every such test a hard timeout so a hang fails loudly. **A hanging concurrency test is more likely a harness deadlock than a service defect** — written into the tester's instructions before the tests existed. LEARN-005.

---

## What this record says about the process

Thirteen defects were caught before any of them could reach a running service. Grouped by when:

| Caught | Count | By what |
|---|---|---|
| Before code existed | 7 | Design review and requirements decomposition |
| While implementing | 5 | Running the failure cases rather than assuming them |
| In the harness | 1 | A test that hung instead of passing |

Two patterns are worth carrying forward. **Writing the correctness argument down made it falsifiable** — four of the design defects were found by someone reading a proof and checking it against the SQL it claimed to prove, which is impossible if the argument lives only in someone's head. And **verifying by execution rather than by inspection** found every one of the implementation defects; each was in a path that looked correct and was not.

---

## Still open

| Item | Status |
|---|---|
| The per-user limit is per principal, and guests are free | Bounded by rate limiting, not closed. A payment step or verified identity is what closes it (RISK-014) |
| Only the claim path has been adversarially reviewed | Auth, shows and the rate limiter have tests and no review |
| CI has never run | GitHub Actions is not enabled for the repository |
| Double log line on an unhandled exception | Both lines are JSON; one lacks a request id. The designed fix is an exception-boundary middleware |
| The full-scale burst on the live instance | Passed live at the default size, and at 20,000 buyers locally. The free instance serves about 19 bookings a second and cannot be scaled from here |

Everything else that is designed and unbuilt is in [17-future-scope.md](17-future-scope.md).
