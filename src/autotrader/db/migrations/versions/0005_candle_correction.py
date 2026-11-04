"""Controlled, audited correction path for closed candles.

Migration 0004 made closed candles immutable for everyone - including the
owner - so a bar found to be corrupt (e.g. frozen from partial data before the
H1 fix) could not be repaired at all. This replaces the trigger function with:

* default: any UPDATE of a closed candle is still rejected;
* correction: allowed only if ALL of
  - a correction ticket is set for the transaction
    (``SET LOCAL autotrader.correction_ticket = '<ticket>'``),
  - the session user is NOT a member of autotrader_writer / autotrader_reader
    (the collector and the API can never use this path, even if they set the
    ticket),
  - the bar's identity (coin, interval, open_time, close_time) is unchanged and
    it stays closed;
* every correction is written to the append-only ``audit_log`` by the trigger
  itself (before/after row, ticket, user).

Tooling: ``autotrader repair-candles`` (dry-run by default; --apply requires a
ticket and a backup directory).

Revision ID: 0005_candle_correction
Revises: 0004_closed_candles_immutable
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_candle_correction"
down_revision: str | None = "0004_closed_candles_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_FUNCTION = """
CREATE OR REPLACE FUNCTION forbid_closed_candle_update() RETURNS trigger AS $$
DECLARE
    ticket text := nullif(current_setting('autotrader.correction_ticket', true), '');
    service_role boolean;
BEGIN
    IF NOT OLD.is_closed THEN
        RETURN NEW;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM pg_auth_members m
        JOIN pg_roles g ON g.oid = m.roleid
        JOIN pg_roles u ON u.oid = m.member
        WHERE u.rolname = session_user
          AND g.rolname IN ('autotrader_writer', 'autotrader_reader')
    ) INTO service_role;
    IF ticket IS NULL OR service_role THEN
        RAISE EXCEPTION 'closed candle %/% % is immutable', OLD.coin, OLD.interval, OLD.open_time;
    END IF;
    IF NEW.coin IS DISTINCT FROM OLD.coin OR NEW.interval IS DISTINCT FROM OLD.interval
       OR NEW.open_time IS DISTINCT FROM OLD.open_time
       OR NEW.close_time IS DISTINCT FROM OLD.close_time OR NOT NEW.is_closed THEN
        RAISE EXCEPTION 'correction may not change bar identity or reopen a closed bar';
    END IF;
    INSERT INTO audit_log (actor, action, entity, before, after)
    VALUES (session_user, 'CORRECT_CLOSED_CANDLE ticket=' || ticket,
            'candles:' || OLD.coin || '/' || OLD.interval || '/' || OLD.open_time,
            to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

OLD_FUNCTION = """
CREATE OR REPLACE FUNCTION forbid_closed_candle_update() RETURNS trigger AS $$
BEGIN
    IF OLD.is_closed THEN
        RAISE EXCEPTION 'closed candle %/% % is immutable',
            OLD.coin, OLD.interval, OLD.open_time;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(NEW_FUNCTION)


def downgrade() -> None:
    op.execute(OLD_FUNCTION)
