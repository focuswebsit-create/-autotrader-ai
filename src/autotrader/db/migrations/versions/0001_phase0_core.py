"""Phase 0 core tables: market_metadata, system_events, audit_log, system_config.

Also installs PostgreSQL guards:
* ``system_config.paper_only`` can never be set to anything but ``true``.
* ``audit_log`` is append-only (UPDATE/DELETE rejected).

Revision ID: 0001_phase0_core
Revises:
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase0_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(38, 18)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "market_metadata",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("dex", sa.String(32), nullable=False),
        sa.Column("coin", sa.String(64), nullable=False),
        sa.Column("dex_index", sa.Integer(), nullable=False),
        sa.Column("asset_index_in_dex", sa.Integer(), nullable=False),
        sa.Column("sz_decimals", sa.Integer(), nullable=False),
        sa.Column("max_price_decimals", sa.Integer(), nullable=False),
        sa.Column("max_leverage", sa.Integer(), nullable=False),
        sa.Column("only_isolated", sa.Boolean(), nullable=False),
        sa.Column("margin_mode", sa.String(32)),
        sa.Column("mark_px", PRICE, nullable=False),
        sa.Column("oracle_px", PRICE, nullable=False),
        sa.Column("mid_px", PRICE),
        sa.Column("funding_rate", PRICE, nullable=False),
        sa.Column("open_interest", PRICE, nullable=False),
        sa.Column("oracle_mark_divergence_bps", sa.Numeric(18, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("warnings", JSONB, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("source", sa.String(256), nullable=False),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('ACTIVE','DEGRADED')", name="ck_market_metadata_status"),
        sa.CheckConstraint("mark_px > 0 AND oracle_px > 0", name="ck_market_metadata_prices"),
    )
    op.create_index("ix_market_metadata_coin", "market_metadata", ["coin"])
    op.create_index(
        "ix_market_metadata_coin_discovered", "market_metadata", ["coin", "discovered_at"]
    )

    op.create_table(
        "system_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')",
            name="ck_system_events_severity",
        ),
    )
    op.create_index("ix_system_events_event_type", "system_events", ["event_type"])
    op.create_index("ix_system_events_occurred_at", "system_events", ["occurred_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity", sa.String(128), nullable=False),
        sa.Column("before", JSONB),
        sa.Column("after", JSONB),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "system_config",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_by", sa.String(128), nullable=False, server_default="system"),
        sa.CheckConstraint(
            "key <> 'paper_only' OR value = 'true'::jsonb", name="ck_system_config_paper_only"
        ),
    )

    op.execute(
        "INSERT INTO system_config (key, value, updated_by) VALUES "
        "('paper_only', 'true'::jsonb, 'migration:0001'), "
        "('initial_equity_usdc', '\"1000\"'::jsonb, 'migration:0001')"
    )
    op.execute(
        """
        CREATE FUNCTION forbid_paper_only_delete() RETURNS trigger AS $$
        BEGIN
            IF OLD.key = 'paper_only' THEN
                RAISE EXCEPTION 'paper_only config row cannot be deleted (PAPER-ONLY system)';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_forbid_paper_only_delete BEFORE DELETE ON system_config "
        "FOR EACH ROW EXECUTE FUNCTION forbid_paper_only_delete();"
    )
    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.execute("DROP TRIGGER IF EXISTS trg_forbid_paper_only_delete ON system_config")
    op.execute("DROP FUNCTION IF EXISTS forbid_paper_only_delete()")
    op.drop_table("system_config")
    op.drop_table("audit_log")
    op.drop_index("ix_system_events_occurred_at", table_name="system_events")
    op.drop_index("ix_system_events_event_type", table_name="system_events")
    op.drop_table("system_events")
    op.drop_index("ix_market_metadata_coin_discovered", table_name="market_metadata")
    op.drop_index("ix_market_metadata_coin", table_name="market_metadata")
    op.drop_table("market_metadata")
