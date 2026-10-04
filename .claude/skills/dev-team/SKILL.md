---
name: dev-team
description: Operating protocol for the seat-reservation dev team — role boundaries, stage gates, handoff contracts, the decision ledger, and the self-enhancement rule. Load at the start of any work session on this project, and before delegating to supervisor/business-analyst/architect/backend-dev/tester/grill.
---

# Dev team protocol

## Roster and boundaries

| Agent | Owns | Must not |
|---|---|---|
| `supervisor` | Stage sequencing, delegation, gate enforcement, ledger integrity | Write production code |
| `business-analyst` | `mds/01-requirements.md`, acceptance criteria, traceability IDs | Choose mechanisms or schemas |
| `architect` | `mds/02`–`mds/13`, ADRs, invariants, schema, module layout | Implement endpoints |
| `backend-dev` | Code under `app/`, migrations, Dockerfile | Change a documented decision silently |
| `tester` | `tests/`, `burst/`, reconciliation checks | Weaken an assertion to make it pass |
| `grill` | Adversarial review of design and code | Approve its own fixes |

Single rule that resolves most conflicts: **the `mds/` docs are the specification.** Code that disagrees with a doc is a bug in one of them; the discrepancy gets resolved and recorded, never left implicit.

## Stage gates

Work proceeds in the stages listed in `mds/14-stage-plan.md`. A stage is **done** only when all of:

1. Acceptance criteria for that stage pass, each traced to a `REQ-*` ID.
2. `tester` has committed tests that fail if the stage's behaviour regresses.
3. `grill` has reviewed and every finding is fixed, refuted in writing, or logged as accepted risk.
4. The stage is committed. Commits are incremental and scoped — one concern per commit.
5. Deployment still comes up healthy (from Stage 0 onward, the live URL is never left broken).

Do not start stage N+1 with stage N's gate open. If a later stage reveals an earlier stage was wrong, reopen it explicitly in the ledger rather than patching around it.

## Handoff contract

Every handoff names: the artifact produced, the `REQ-*`/`ADR-*` IDs it touches, what the receiver must verify, and any assumption the receiver is entitled to reject. A handoff without a verification instruction is incomplete.

## Decision ledger

`mds/99-ledger.md` is append-only. Every entry is one of:

- `ADR-NNN` — a decision: context, options, choice, consequences, date.
- `LEARN-NNN` — something discovered the hard way (a Postgres behaviour, a platform limit, a failed approach) with the evidence.
- `RISK-NNN` — an accepted risk with its trigger and mitigation.

Never delete an entry. Supersede it with a new one that references the old ID. An ADR is only reversed by another ADR.

## Decision highlights

`mds/16-decision-highlights.md` is the curated companion to the ledger. The ledger records *what* was decided; this records **how it came about and who drove it** — the judgment calls, the defects caught before shipping, and what was nearly shipped instead.

Append an entry when any of these happen:

- A decision changes the design, and the reasoning that produced it is worth keeping.
- A defect is caught before it ships — record the source (design review, requirements decomposition, implementation, test harness) and the stage at which it was caught.
- An assumption is replaced by evidence.
- A direction from the user materially changes the design, including when it corrects a recommendation already on the table. Record these honestly; they are the provenance the write-up's AI-usage section needs, and reconstructing them later produces a flattering fiction instead of a record.

Do not append routine implementation work. This file is the signal, not the log — a highlights document that lists everything highlights nothing.

## Self-enhancement rule

These skills are expected to grow as the project does. When any agent learns something durable — a convention that prevented a bug, a Postgres semantic that matters, a platform constraint, a review finding that recurred — it must do **both**:

1. Append a `LEARN-NNN` entry to `mds/99-ledger.md` with the evidence.
2. Edit the relevant skill file (`dev-team`, `fastapi-conventions`, `concurrency-correctness`) so the next session starts with that knowledge, and add the new rule to the **Enhancement log** at the bottom of that skill.

Thresholds, so the skills stay sharp rather than bloated:

- A rule earns a place only if following it would have prevented a real defect, or if violating it is tempting.
- Prefer editing an existing rule to appending a near-duplicate.
- A rule that is now enforced by a test, a type, or a database constraint moves out of prose and into the enforcement mechanism; cite the enforcement and delete the prose.
- Reverse a rule when it stops being true. Stale guidance is worse than none.

## Commit discipline

The commit history is part of the deliverable. One logical change per commit, imperative subject, body explaining *why* when it is not obvious. Never a single squashed "implement everything" commit. Never commit a broken build on `main`.

## Enhancement log

- `2026-10-03` — Initial protocol: roster, stage gates, ledger, self-enhancement rule.
- `2026-10-03` — Added the decision-highlights companion to the ledger, with its append triggers. Rationale: four design defects were found by someone reading a written correctness argument and checking it against the SQL it claimed to prove — impossible if the argument lives only in someone's head — and every implementation defect was found by executing the failure case rather than inspecting it. Both patterns are worth recording as practice, not just as outcomes.
