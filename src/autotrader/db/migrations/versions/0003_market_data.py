"""Phase 1 market data: candles, data-quality issues, ingestion runs, heartbeats,
plus least-privilege database roles.

Roles (NOLOGIN groups; login users are created by infra/db/roles.sql):
* ``autotrader_reader``: SELECT on all application tables (used by the API).
* ``autotrader_writer``: SELECT everywhere; INSERT/UPDATE only on the tables the
  collector writes. No DELETE, TRUNCATE or DDL anywhere, and no write access to
  ``system_config`` or ``audit_log``.

If the migrating user may not create roles, role creation is skipped with a
NOTICE (grants are then applied only to roles that already exist).

Revision ID: 0003_market_data
Revises: 0002_hardening
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_market_data"
down_revision: str | None = "0002_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APP_TABLES = (
    "market_metadata",
    "system_events",
    "audit_log",
    "system_config",
    "candles",
    "data_quality_issues",
    "ingestion_runs",
    "service_heartbeats",
)
WRITER_INSERT = (
    "market_metadata",
    "system_events",
    "candles",
    "data_quality_issues",
    "ingestion_runs",
    "service_heartbeats",
)
WRITER_UPDATE = ("candles", "data_quality_issues", "ingestion_runs", "service_heartbeats")
ROLES = ("autotrader_reader", "autotrader_writer")


def _create_roles() -> None:
    for role in ROLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE NOTICE 'cannot create role {role}; create it via infra/db/roles.sql';
            END
            $$;
            """
        )


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


def _apply_grants() -> None:
    for role in ROLES:
        _grant(f"GRANT USAGE ON SCHEMA public TO {role}", role)
        _grant(f"GRANT SELECT ON {', '.join(APP_TABLES)} TO {role}", role)
    _grant(f"GRANT INSERT ON {', '.join(WRITER_INSERT)} TO autotrader_writer", "autotrader_writer")
    _grant(f"GRANT UPDATE ON {', '.join(WRITER_UPDATE)} TO autotrader_writer", "autotrader_writer")
    _grant(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO autotrader_writer",
        "autotrader_writer",
    )


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("high", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("low", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("close", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("volume", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=True),
        sa.Column("is_closed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingestion_run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.CheckConstraint("interval IN ('30m','1h')", name="ck_candles_interval"),
        sa.CheckConstraint("source IN ('rest','ws')", name="ck_candles_source"),
        sa.CheckConstraint("close_time > open_time", name="ck_candles_times"),
        sa.CheckConstraint(
            "high >= open AND high >= close AND low <= open AND low <= close AND high >= low",
            name="ck_candles_ohlc",
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_candles_positive"
        ),
        sa.CheckConstraint("volume >= 0", name="ck_candles_volume"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("coin", "interval", "open_time", name="uq_candles_bar"),
    )
    op.create_index(
        "ix_candles_lookup", "candles", ["coin", "interval", "is_closed", "open_time"], unique=False
    )
    op.create_table(
        "data_quality_issues",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("issue_type", sa.String(length=32), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_bars", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("severity IN ('INFO','WARNING','ERROR')", name="ck_dqi_severity"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "coin", "interval", "issue_type", "open_time", name="uq_data_quality_issue"
        ),
    )
    op.create_index(
        "ix_dqi_open", "data_quality_issues", ["coin", "interval", "resolved_at"], unique=False
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("coin", sa.String(length=64), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("first_open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('backfill','gap_fill','finalize')", name="ck_ingest_kind"),
        sa.CheckConstraint("status IN ('RUNNING','OK','FAILED')", name="ck_ingest_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_runs_recent",
        "ingestion_runs",
        ["coin", "interval", "started_at"],
        unique=False,
    )
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("ws_state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ws_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_candle_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconnects", sa.Integer(), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('STARTING','RUNNING','DEGRADED','STOPPED')", name="ck_heartbeat_state"
        ),
        sa.CheckConstraint(
            "ws_state IN ('DISCONNECTED','CONNECTING','CONNECTED','STALE')",
            name="ck_heartbeat_ws_state",
        ),
        sa.PrimaryKeyConstraint("service"),
    )

    _create_roles()
    _apply_grants()


def downgrade() -> None:
    for role in ROLES:
        _grant(f"REVOKE ALL ON {', '.join(APP_TABLES)} FROM {role}", role)
        _grant(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}", role)
        _grant(f"REVOKE USAGE ON SCHEMA public FROM {role}", role)
    op.drop_table("service_heartbeats")
    op.drop_index("ix_ingestion_runs_recent", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index("ix_dqi_open", table_name="data_quality_issues")
    op.drop_table("data_quality_issues")
    op.drop_index("ix_candles_lookup", table_name="candles")
    op.drop_table("candles")
