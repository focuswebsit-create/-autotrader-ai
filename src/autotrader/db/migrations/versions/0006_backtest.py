"""Phase 2: recorded funding rates and immutable backtest runs/trades.

* ``funding_rates``   - hourly funding from Hyperliquid ``fundingHistory`` (append-only).
* ``backtest_runs``   - one row per research-protocol run: config, data
  fingerprint, code version, verdict and the full JSON report.
* ``backtest_trades`` - the primary run's trades (FK to the run).

Runs and trades are immutable: a trigger rejects UPDATE and DELETE for every
user, and the service roles only get SELECT (+ INSERT for the worker).

Revision ID: 0006_backtest
Revises: 0005_candle_correction
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_backtest"
down_revision: str | None = "0005_candle_correction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ("funding_rates", "backtest_runs", "backtest_trades")
ROLES = ("autotrader_reader", "autotrader_writer")
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
PRICE = sa.Numeric(precision=38, scale=18)
JSONB = postgresql.JSONB(astext_type=sa.Text())


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
        "funding_rates",
        sa.Column("id", PK, nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate", PRICE, nullable=False),
        sa.Column("premium", PRICE, nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rate > -1 AND rate < 1", name="ck_funding_rate_range"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("coin", "hour", name="uq_funding_rates_hour"),
    )
    op.create_index("ix_funding_rates_lookup", "funding_rates", ["coin", "hour"])

    op.create_table(
        "backtest_runs",
        sa.Column("id", PK, nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("params_hash", sa.String(length=64), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("data_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("report", JSONB, nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('PASS','FAIL','INCONCLUSIVE','INVALID')", name="ck_backtest_runs_verdict"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backtest_runs_strategy", "backtest_runs", ["coin", "strategy_id", "created_at"]
    )

    op.create_table(
        "backtest_trades",
        sa.Column("id", PK, nullable=False),
        sa.Column("run_id", PK, nullable=False),
        sa.Column("trade_no", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", PRICE, nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_price", PRICE, nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=False),
        sa.Column("size", PRICE, nullable=False),
        sa.Column("net_pnl", PRICE, nullable=False),
        sa.Column("r_multiple", PRICE, nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.CheckConstraint("side IN ('LONG','SHORT')", name="ck_backtest_trades_side"),
        sa.CheckConstraint("exit_time >= entry_time", name="ck_backtest_trades_times"),
        sa.CheckConstraint("entry_time >= signal_time", name="ck_backtest_trades_no_lookahead"),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "trade_no", name="uq_backtest_trades_no"),
    )

    op.execute(
        """
        CREATE FUNCTION forbid_backtest_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable (% rejected)', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in NEW_TABLES:
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
    op.execute("DROP FUNCTION IF EXISTS forbid_backtest_mutation()")
    op.drop_table("backtest_trades")
    op.drop_index("ix_backtest_runs_strategy", table_name="backtest_runs")
    op.drop_table("backtest_runs")
    op.drop_index("ix_funding_rates_lookup", table_name="funding_rates")
    op.drop_table("funding_rates")
