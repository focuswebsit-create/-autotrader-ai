"""Phase 2.5 hardening: a forward test is LIVE by construction.

* ``forward_tests.created_at`` and ``forward_decisions.evaluated_at`` must be within
  5 minutes of the database clock at INSERT time. Historical replays (simulating
  the past "as if live") therefore can't be written into the production journal;
  they exist only in tests and in the backtest tables.
* ``service_heartbeats.ws_state`` gains ``NOT_APPLICABLE`` for services without a
  WebSocket (the forward worker), so monitoring can track every service.

Revision ID: 0008_forward_realtime_guard
Revises: 0007_forward_tests
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_forward_realtime_guard"
down_revision: str | None = "0007_forward_tests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE FUNCTION forward_require_wallclock() RETURNS trigger AS $$
DECLARE
    ts timestamptz;
BEGIN
    IF TG_TABLE_NAME = 'forward_tests' THEN
        ts := NEW.created_at;
    ELSE
        ts := NEW.evaluated_at;
    END IF;
    IF ts < clock_timestamp() - interval '5 minutes'
       OR ts > clock_timestamp() + interval '5 minutes' THEN
        RAISE EXCEPTION '% must be written in real time (% vs db clock %); replays are not '
            'allowed in the forward journal', TG_TABLE_NAME, ts, clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    for table in ("forward_tests", "forward_decisions"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_wallclock BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forward_require_wallclock();"
        )
    op.execute("ALTER TABLE service_heartbeats DROP CONSTRAINT ck_heartbeat_ws_state")
    op.execute(
        "ALTER TABLE service_heartbeats ADD CONSTRAINT ck_heartbeat_ws_state CHECK "
        "(ws_state IN ('DISCONNECTED','CONNECTING','CONNECTED','STALE','NOT_APPLICABLE'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM service_heartbeats WHERE ws_state = 'NOT_APPLICABLE'")
    op.execute("ALTER TABLE service_heartbeats DROP CONSTRAINT ck_heartbeat_ws_state")
    op.execute(
        "ALTER TABLE service_heartbeats ADD CONSTRAINT ck_heartbeat_ws_state CHECK "
        "(ws_state IN ('DISCONNECTED','CONNECTING','CONNECTED','STALE'))"
    )
    for table in ("forward_tests", "forward_decisions"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_wallclock ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forward_require_wallclock()")
