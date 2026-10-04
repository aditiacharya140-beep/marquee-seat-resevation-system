"""Initial schema: the whole of mds/03-data-model.md except audit_log.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = (
    """
    CREATE TABLE users (
        id            UUID PRIMARY KEY,
        email         TEXT,
        password_hash TEXT,
        role          TEXT NOT NULL CONSTRAINT ck_users_role CHECK (role IN ('admin','user')),
        is_guest      BOOLEAN NOT NULL DEFAULT false,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        request_id    UUID,
        CONSTRAINT ck_users_creds CHECK (
            (is_guest AND email IS NULL AND password_hash IS NULL)
         OR (NOT is_guest AND email IS NOT NULL AND password_hash IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX uq_users_email ON users (LOWER(email)) WHERE email IS NOT NULL",
    """
    CREATE TABLE shows (
        id               UUID PRIMARY KEY,
        event_kind       TEXT NOT NULL,
        name             TEXT NOT NULL,
        price_paise      BIGINT NOT NULL CONSTRAINT ck_shows_price CHECK (price_paise >= 0),
        currency         CHAR(3) NOT NULL,
        per_user_limit   INT NOT NULL CONSTRAINT ck_shows_limit CHECK (per_user_limit > 0),
        hold_ttl_seconds INT NOT NULL CONSTRAINT ck_shows_ttl CHECK (hold_ttl_seconds > 0),
        total_seats      INT NOT NULL CONSTRAINT ck_shows_total CHECK (total_seats > 0),
        status           TEXT NOT NULL
            CONSTRAINT ck_shows_status CHECK (status IN ('draft','on_sale','closed')),
        sales_open_at    TIMESTAMPTZ,
        sales_close_at   TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        request_id       UUID
    )
    """,
    """
    CREATE TABLE seats (
        id              UUID PRIMARY KEY,
        show_id         UUID NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        label           TEXT NOT NULL,
        section         TEXT,
        price_paise     BIGINT CONSTRAINT ck_seats_price CHECK (price_paise >= 0),
        status          TEXT NOT NULL DEFAULT 'available'
            CONSTRAINT ck_seats_status CHECK (status IN ('available','held','confirmed')),
        held_by         UUID REFERENCES users (id),
        reservation_id  UUID,
        hold_expires_at TIMESTAMPTZ,
        version         BIGINT NOT NULL DEFAULT 0,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        request_id      UUID,
        CONSTRAINT uq_seats_show_label UNIQUE (show_id, label),
        CONSTRAINT ck_seats_hold_coherent CHECK (
            (status = 'available' AND held_by IS NULL
                AND reservation_id IS NULL AND hold_expires_at IS NULL)
         OR (status = 'held' AND held_by IS NOT NULL
                AND reservation_id IS NOT NULL AND hold_expires_at IS NOT NULL)
         OR (status = 'confirmed' AND held_by IS NOT NULL
                AND reservation_id IS NOT NULL AND hold_expires_at IS NULL))
    )
    """,
    "CREATE INDEX ix_seats_claimable ON seats (show_id, label) WHERE status <> 'confirmed'",
    "CREATE INDEX ix_seats_by_holder ON seats (show_id, held_by) WHERE held_by IS NOT NULL",
    """
    CREATE TABLE idempotency_keys (
        id                  UUID PRIMARY KEY,
        user_id             UUID NOT NULL REFERENCES users (id),
        key                 TEXT NOT NULL,
        scope               TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        state               TEXT NOT NULL
            CONSTRAINT ck_idem_state CHECK (state IN ('in_progress','completed')),
        status_code         INT,
        response_body       JSONB,
        reservation_id      UUID,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at        TIMESTAMPTZ,
        expires_at          TIMESTAMPTZ NOT NULL,
        request_id          UUID,
        CONSTRAINT uq_idem_user_key UNIQUE (user_id, key)
    )
    """,
    "CREATE INDEX ix_idem_expiry ON idempotency_keys (expires_at)",
    """
    CREATE TABLE reservations (
        id                 UUID PRIMARY KEY,
        show_id            UUID NOT NULL REFERENCES shows (id),
        user_id            UUID NOT NULL REFERENCES users (id),
        status             TEXT NOT NULL CONSTRAINT ck_reservations_status
            CHECK (status IN ('held','confirmed','cancelled','expired')),
        seat_count         INT NOT NULL CONSTRAINT ck_reservations_count CHECK (seat_count > 0),
        amount_paise       BIGINT NOT NULL
            CONSTRAINT ck_reservations_amount CHECK (amount_paise >= 0),
        currency           CHAR(3) NOT NULL,
        hold_expires_at    TIMESTAMPTZ,
        confirmed_at       TIMESTAMPTZ,
        cancelled_at       TIMESTAMPTZ,
        expired_at         TIMESTAMPTZ,
        idempotency_key_id UUID REFERENCES idempotency_keys (id) ON DELETE SET NULL,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        request_id         UUID
    )
    """,
    "CREATE INDEX ix_reservations_user_show ON reservations (user_id, show_id, status)",
    """
    CREATE TABLE reservation_seats (
        reservation_id UUID NOT NULL REFERENCES reservations (id),
        seat_id        UUID NOT NULL REFERENCES seats (id) ON DELETE CASCADE,
        label          TEXT NOT NULL,
        price_paise    BIGINT NOT NULL,
        released_at    TIMESTAMPTZ,
        PRIMARY KEY (reservation_id, seat_id)
    )
    """,
    # The backstop: a second simultaneous active claim on one seat is unrepresentable.
    # The predicate cannot also say "unexpired" -- now() is not IMMUTABLE (LEARN-008).
    """
    CREATE UNIQUE INDEX uq_seat_active_claim
        ON reservation_seats (seat_id) WHERE released_at IS NULL
    """,
    """
    CREATE TABLE user_show_quota (
        user_id    UUID NOT NULL REFERENCES users (id),
        show_id    UUID NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, show_id)
    )
    """,
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("migrations are forward-only (mds/03-data-model.md)")
