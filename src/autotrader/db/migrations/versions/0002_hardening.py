"""Hardening after Phase 0 QA review.

* ``system_events.subject`` (+ index) so events can be filtered per market and
  ordered by id instead of by timestamps from two different clocks.
* ``system_config``: the ``paper_only`` row can no longer be renamed (UPDATE of
  ``key``) and the table cannot be TRUNCATEd.
* ``audit_log``: TRUNCATE is rejected too (row triggers don't fire on TRUNCATE).

Note: the table owner can still DROP/ALTER these guards. Using a separate,
non-owner runtime role is a deployment task (see docs/DECISIONS.md ADR-010).

Revision ID: 0002_hardening
Revises: 0001_phase0_core
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_hardening"
down_revision: str | None = "0001_phase0_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("system_events", sa.Column("subject", sa.String(64), nullable=True))
    # Backfill: failures carry details.coin; 0001-era successes only have the coin
    # as the first word of the message ("xyz:CL discovered with status ...").
    op.execute(
        """
        UPDATE system_events
        SET subject = COALESCE(
            details->>'coin',
            CASE WHEN event_type = 'MARKET_DISCOVERY_OK' THEN split_part(message, ' ', 1) END
        )
        WHERE event_type LIKE 'MARKET_DISCOVERY_%'
        """
    )
    op.create_index(
        "ix_system_events_type_subject_id", "system_events", ["event_type", "subject", "id"]
    )

    op.execute(
        """
        CREATE FUNCTION forbid_paper_only_rename() RETURNS trigger AS $$
        BEGIN
            IF OLD.key = 'paper_only' AND NEW.key IS DISTINCT FROM OLD.key THEN
                RAISE EXCEPTION 'paper_only config row cannot be renamed (PAPER-ONLY system)';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_forbid_paper_only_rename BEFORE UPDATE ON system_config "
        "FOR EACH ROW EXECUTE FUNCTION forbid_paper_only_rename();"
    )
    op.execute(
        """
        CREATE FUNCTION forbid_truncate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% cannot be truncated (PAPER-ONLY guard / append-only)', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("system_config", "audit_log"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();"
        )


def downgrade() -> None:
    for table in ("system_config", "audit_log"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_truncate()")
    op.execute("DROP TRIGGER IF EXISTS trg_forbid_paper_only_rename ON system_config")
    op.execute("DROP FUNCTION IF EXISTS forbid_paper_only_rename()")
    op.drop_index("ix_system_events_type_subject_id", table_name="system_events")
    op.drop_column("system_events", "subject")
