"""Phase 2.5: frozen forward tests, append-only decision journal and events.

* ``forward_tests``     - frozen strategy/params/costs/risk/code per test.
* ``forward_decisions`` - one decision per closed 30m bar, with market snapshot.
* ``forward_events``    - STARTED / STOPPED / INTEGRITY_HALT / DATA_GAP / DATA_STALE.

All three are immutable: a trigger rejects UPDATE and DELETE for every user (results
are recomputed from these rows, never stored, so nothing can be edited after the
fact). Service roles get SELECT (+ INSERT for the worker).

Revision ID: 0007_forward_tests
Revises: 0006_backtest
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_forward_tests"
down_revision: str | None = "0006_backtest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ("forward_tests", "forward_decisions", "forward_events")
ROLES = ("autotrader_reader", "autotrader_writer")
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
PRICE = sa.Numeric(precision=38, scale=18)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)


def _grant(sql: str, role: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                {sql};
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    op.create_table(
        "forward_tests",
        sa.Column("id", PK, nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("history_anchor", TS, nullable=False),
        sa.Column("starts_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint("starts_at >= created_at", name="ck_forward_tests_future_start"),
        sa.CheckConstraint("history_anchor < starts_at", name="ck_forward_tests_anchor"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_forward_tests_strategy", "forward_tests", ["coin", "strategy_id"])

    op.create_table(
        "forward_decisions",
        sa.Column("id", PK, nullable=False),
        sa.Column("test_id", PK, nullable=False),
        sa.Column("bar_open_time", TS, nullable=False),
        sa.Column("bar_close_time", TS, nullable=False),
        sa.Column("evaluated_at", TS, nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("atr", PRICE, nullable=True),
        sa.Column("rationale", JSONB, nullable=False),
        sa.Column("market", JSONB, nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint("side IS NULL OR side IN ('LONG','SHORT')", name="ck_fd_side"),
        sa.CheckConstraint("evaluated_at >= bar_close_time", name="ck_fd_no_lookahead"),
        sa.ForeignKeyConstraint(["test_id"], ["forward_tests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("test_id", "bar_open_time", name="uq_forward_decisions_bar"),
    )
    op.create_index(
        "ix_forward_decisions_signals",
        "forward_decisions",
        ["test_id", "side", "bar_open_time"],
    )

    op.create_table(
        "forward_events",
        sa.Column("id", PK, nullable=False),
        sa.Column("test_id", PK, nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.CheckConstraint(
            "event IN ('STARTED','STOPPED','INTEGRITY_HALT','DATA_GAP','DATA_STALE')",
            name="ck_forward_events_event",
        ),
        sa.ForeignKeyConstraint(["test_id"], ["forward_tests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_forward_events_test", "forward_events", ["test_id", "id"])

    # A decision may only be stored for a bar that opens at/after the test start.
    op.execute(
        """
        CREATE FUNCTION forward_decision_in_test_period() RETURNS trigger AS $$
        DECLARE
            start_ts timestamptz;
        BEGIN
            SELECT starts_at INTO start_ts FROM forward_tests WHERE id = NEW.test_id;
            IF NEW.bar_open_time < start_ts THEN
                RAISE EXCEPTION 'decision for bar % precedes forward test start %',
                    NEW.bar_open_time, start_ts;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_forward_decision_period BEFORE INSERT ON forward_decisions "
        "FOR EACH ROW EXECUTE FUNCTION forward_decision_in_test_period();"
    )
    for table in NEW_TABLES:
        # reuses forbid_backtest_mutation() from migration 0006
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_backtest_mutation();"
        )

    tables = ", ".join(NEW_TABLES)
    for role in ROLES:
        _grant(f"GRANT SELECT ON {tables} TO {role}", role)
    _grant(f"GRANT INSERT ON {tables} TO autotrader_writer", "autotrader_writer")
    _grant(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO autotrader_writer",
        "autotrader_writer",
    )


def downgrade() -> None:
    tables = ", ".join(NEW_TABLES)
    for role in ROLES:
        _grant(f"REVOKE ALL ON {tables} FROM {role}", role)
    for table in NEW_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_forward_decision_period ON forward_decisions")
    op.execute("DROP FUNCTION IF EXISTS forward_decision_in_test_period()")
    op.drop_index("ix_forward_events_test", table_name="forward_events")
    op.drop_table("forward_events")
    op.drop_index("ix_forward_decisions_signals", table_name="forward_decisions")
    op.drop_table("forward_decisions")
    op.drop_index("ix_forward_tests_strategy", table_name="forward_tests")
    op.drop_table("forward_tests")
