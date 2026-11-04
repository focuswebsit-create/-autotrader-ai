"""Enforce ADR-015 in the database: a closed candle can never be modified.

Until now immutability of closed bars was guaranteed only by application code;
``autotrader_writer`` has UPDATE on ``candles`` (it must, to update forming
bars). This trigger rejects any UPDATE of a row whose OLD.is_closed is true,
whoever issues it (Phase 1 final review, finding L1).

Revision ID: 0004_closed_candles_immutable
Revises: 0003_market_data
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_closed_candles_immutable"
down_revision: str | None = "0003_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION forbid_closed_candle_update() RETURNS trigger AS $$
        BEGIN
            IF OLD.is_closed THEN
                RAISE EXCEPTION 'closed candle %/% % is immutable',
                    OLD.coin, OLD.interval, OLD.open_time;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_closed_candle_immutable BEFORE UPDATE ON candles "
        "FOR EACH ROW EXECUTE FUNCTION forbid_closed_candle_update();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_closed_candle_immutable ON candles")
    op.execute("DROP FUNCTION IF EXISTS forbid_closed_candle_update()")
