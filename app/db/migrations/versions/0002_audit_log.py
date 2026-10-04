"""The audit trail: one row per request (REQ-046).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_STATEMENTS = (
    # No foreign keys: a check per insert would add contention for nothing, and a row
    # naming a deleted user is still evidence. BIGSERIAL because the rows are written
    # in batches and read newest-first.
    """
    CREATE TABLE audit_log (
        id              BIGSERIAL PRIMARY KEY,
        request_id      UUID,
        occurred_at     TIMESTAMPTZ NOT NULL,
        method          TEXT NOT NULL,
        path            TEXT NOT NULL,
        route           TEXT NOT NULL,
        status_code     INT NOT NULL,
        duration_ms     INT NOT NULL,
        user_id         UUID,
        is_guest        BOOLEAN,
        outcome_code    TEXT,
        show_id         UUID,
        seat_labels     TEXT[],
        idempotency_key TEXT,
        client_ip       TEXT
    )
    """,
    "CREATE INDEX ix_audit_occurred ON audit_log (occurred_at DESC)",
    "CREATE INDEX ix_audit_request ON audit_log (request_id)",
    """
    CREATE INDEX ix_audit_outcome ON audit_log (outcome_code, occurred_at DESC)
        WHERE outcome_code IS NOT NULL
    """,
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (mds/03-data-model.md)")
