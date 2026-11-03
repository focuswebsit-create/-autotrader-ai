"""Alembic migrations + DB guards on real PostgreSQL.

These tests DROP AND RECREATE all tables. They therefore use a dedicated variable,
``AUTOTRADER_TEST_DATABASE_URL``, and refuse any database whose name does not end
in ``_test`` (review finding M4), so they can never wipe a dev/prod database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from autotrader.common.config import Settings
from autotrader.db.repository import latest_discovery
from autotrader.worker.discovery_job import run_discovery_once
from tests.conftest import FakeInfo

DB_URL = os.environ.get("AUTOTRADER_TEST_DATABASE_URL", "")


def _is_safe_test_db(url: str) -> bool:
    if not url.startswith("postgresql"):
        return False
    return make_url(url).database is not None and str(make_url(url).database).endswith("_test")


pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not _is_safe_test_db(DB_URL),
        reason="set AUTOTRADER_TEST_DATABASE_URL to a PostgreSQL database named *_test",
    ),
]


@pytest.fixture
def migrated() -> Iterator[str]:
    assert _is_safe_test_db(DB_URL)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield DB_URL
    command.downgrade(cfg, "base")


def test_upgrade_downgrade_roundtrip(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.connect() as c:
        tables = set(
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars()
        )
    assert {"market_metadata", "system_events", "audit_log", "system_config"} <= tables


def test_paper_only_row_cannot_be_disabled(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT value FROM system_config WHERE key='paper_only'")).scalar_one()
            is True
        )
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE system_config SET value='false'::jsonb WHERE key='paper_only'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM system_config WHERE key='paper_only'"))


def test_audit_log_append_only(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_log(actor,action,entity) VALUES ('t','a','e')"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE audit_log SET actor='x'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM audit_log"))


def test_worker_persists_discovery(migrated: str, fake_info: FakeInfo) -> None:
    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    assert run_discovery_once(Settings(), fake_info, sessions).ok
    with sessions() as s:
        row = latest_discovery(s, "xyz:CL")
        assert row is not None
        assert str(row.mark_px).startswith("94.913")
        assert row.status == "ACTIVE"


def test_paper_only_row_cannot_be_renamed(migrated: str) -> None:
    """Review M2: renaming the key used to bypass the CHECK constraint."""
    engine = create_engine(migrated)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(
            text(
                "UPDATE system_config SET key='paper_only_disabled', value='false'::jsonb "
                "WHERE key='paper_only'"
            )
        )
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT value FROM system_config WHERE key='paper_only'")).scalar_one()
            is True
        )


@pytest.mark.parametrize("table", ["system_config", "audit_log"])
def test_guarded_tables_cannot_be_truncated(migrated: str, table: str) -> None:
    """Review M2: row-level triggers don't fire on TRUNCATE."""
    engine = create_engine(migrated)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(f"TRUNCATE {table}"))


def test_other_config_rows_still_editable(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(
            text("UPDATE system_config SET value='\"2000\"'::jsonb WHERE key='initial_equity_usdc'")
        )


def test_orm_models_match_migrations(migrated: str) -> None:
    """No drift between SQLAlchemy models and Alembic head."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from autotrader.db.models import Base

    engine = create_engine(migrated)
    with engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []


def test_extreme_values_never_reach_the_database(
    migrated: str, perp_dexs: Any, meta_xyz: Any
) -> None:
    """Review M1: values the NUMERIC columns can't hold are rejected before insert."""
    import copy

    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    meta = copy.deepcopy(meta_xyz)
    i = next(n for n, a in enumerate(meta[0]["universe"]) if a["name"] == "xyz:CL")
    meta[1][i]["markPx"] = "1e25"
    meta[1][i]["oraclePx"] = "1e25"
    fake = FakeInfo({"perpDexs": perp_dexs, "metaAndAssetCtxs": meta})
    outcome = run_discovery_once(Settings(), fake, sessions)
    assert not outcome.ok and outcome.error_code == "OUT_OF_RANGE"
    assert outcome.persisted is True
    with sessions() as s:
        assert latest_discovery(s, "xyz:CL") is None


# --------------------------------------------------------------------------- Phase 1

ROLE_PW = "test-only-pw"


def _role_url(role: str) -> str:
    url = make_url(DB_URL)
    return url.set(username=role, password=ROLE_PW).render_as_string(hide_password=False)


@pytest.fixture
def roles(migrated: str) -> dict[str, Any]:
    """Login users exactly as infra/db/roles.sql defines them (group-role membership)."""
    engine = create_engine(migrated)
    with engine.begin() as c:
        for login, group in (
            ("autotrader_api", "autotrader_reader"),
            ("autotrader_worker", "autotrader_writer"),
        ):
            exists = c.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": login}
            ).first()
            if not exists:
                c.execute(text(f"CREATE ROLE {login} LOGIN"))
            c.execute(
                text(f"ALTER ROLE {login} WITH LOGIN NOSUPERUSER INHERIT PASSWORD '{ROLE_PW}'")
            )
            c.execute(text(f"GRANT {group} TO {login}"))
    return {
        "api": create_engine(_role_url("autotrader_api")),
        "worker": create_engine(_role_url("autotrader_worker")),
    }


DENIED = ("permission denied", "must be owner", "insufficientprivilege")


def _denied(engine: Any, sql: str) -> bool:
    try:
        with engine.begin() as c:
            c.execute(text(sql))
    except DBAPIError as exc:
        return any(d in str(exc).lower().replace(" ", "") or d in str(exc).lower() for d in DENIED)
    return False


CANDLE_SQL = (
    "INSERT INTO candles (coin, interval, open_time, close_time, open, high, low, close, volume, "
    "is_closed, source, first_seen_at, updated_at) VALUES ('xyz:CL','30m', "
    "'2026-01-01T00:00:00Z','2026-01-01T00:30:00Z', {o},{h},{l},{c},1, true,'rest', now(), now())"
)


def test_worker_role_least_privilege(roles: dict[str, Any]) -> None:
    w = roles["worker"]
    with w.begin() as c:  # allowed: what the collector actually does
        c.execute(text(CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)))
        c.execute(text("UPDATE candles SET volume = 2"))
        c.execute(
            text(
                "INSERT INTO system_events (event_type, severity, message, details) "
                "VALUES ('X','INFO','m','{}')"
            )
        )
    assert _denied(w, "DELETE FROM candles")
    assert _denied(w, "TRUNCATE candles")
    assert _denied(w, "DROP TABLE candles")
    assert _denied(w, "ALTER TABLE candles DROP CONSTRAINT ck_candles_ohlc")
    assert _denied(w, "UPDATE system_config SET value='false'::jsonb WHERE key='paper_only'")
    assert _denied(w, "INSERT INTO audit_log (actor, action, entity) VALUES ('w','x','y')")
    assert _denied(w, "DELETE FROM system_events")
    assert _denied(w, "CREATE TABLE evil (id int)")


def test_api_role_is_read_only(roles: dict[str, Any]) -> None:
    a = roles["api"]
    with a.connect() as c:
        c.execute(text("SELECT count(*) FROM candles")).scalar_one()
        c.execute(text("SELECT count(*) FROM service_heartbeats")).scalar_one()
    assert _denied(a, CANDLE_SQL.format(o=90, h=91, l=89, c=90.5))
    assert _denied(a, "UPDATE candles SET volume = 3")
    assert _denied(
        a,
        "INSERT INTO service_heartbeats (service, state, ws_state, started_at, "
        "last_heartbeat_at) VALUES ('x','RUNNING','CONNECTED',now(),now())",
    )
    assert _denied(a, "DELETE FROM data_quality_issues")


def test_every_table_is_granted_to_roles(migrated: str) -> None:
    """New tables in future migrations must not be forgotten in the grants."""
    engine = create_engine(migrated)
    with engine.connect() as c:
        tables = (
            c.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename <> 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        for t in tables:
            for role in ("autotrader_reader", "autotrader_writer"):
                ok = c.execute(
                    text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": role, "t": t}
                ).scalar()
                assert ok, f"{role} lacks SELECT on {t}"


@pytest.mark.parametrize(
    "vals",
    [dict(o=0, h=1, l=0, c=1), dict(o=90, h=89, l=88, c=90), dict(o=90, h=91, l=90.5, c=90)],
)
def test_candle_check_constraints(migrated: str, vals: dict[str, float]) -> None:
    engine = create_engine(migrated)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(CANDLE_SQL.format(**vals)))


def test_unique_bar_constraint(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(text(CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)))


def test_postgres_backfill_idempotent_and_exact(migrated: str, snapshot_30m: Any) -> None:
    from decimal import Decimal

    from autotrader.db.market_data import closed_candles
    from autotrader.ingest.rest import backfill
    from autotrader.market.candles import Interval
    from tests.conftest import RECORDED_AT, Clock

    class Info:
        def info(self, payload: dict[str, Any]) -> Any:
            return snapshot_30m

    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    first = backfill(sessions, Info(), coin="xyz:CL", interval=Interval.M30, clock=Clock())
    again = backfill(sessions, Info(), coin="xyz:CL", interval=Interval.M30, clock=Clock())
    assert first.inserted == 201 and again.inserted == 0 and again.updated == 0
    with sessions() as s:
        rows = closed_candles(s, "xyz:CL", Interval.M30, as_of=RECORDED_AT)
        assert len(rows) == 200
        assert rows[0].close == Decimal(snapshot_30m[0]["c"])  # exact NUMERIC round-trip


def test_single_collector_lock(migrated: str) -> None:
    from autotrader.db.market_data import (
        lock_still_held,
        open_lock_connection,
        release_collector_lock,
        try_collector_lock,
    )

    engine = create_engine(migrated)
    a, b = open_lock_connection(migrated), open_lock_connection(migrated)
    try:
        assert try_collector_lock(a) is True
        assert lock_still_held(a) is True
        assert try_collector_lock(b) is False  # a second collector must refuse to start
        # Holding the lock must not leave a transaction open ("idle in transaction").
        with engine.connect() as c:
            states = (
                c.execute(
                    text("SELECT state FROM pg_stat_activity WHERE datname = current_database()")
                )
                .scalars()
                .all()
            )
        assert "idle in transaction" not in states
    finally:
        release_collector_lock(a)
        release_collector_lock(b)
    # Regression: a fresh connection (different backend) can take the lock again,
    # i.e. releasing really released it (no pooled connection kept holding it).
    c2 = open_lock_connection(migrated)
    try:
        assert try_collector_lock(c2) is True
    finally:
        release_collector_lock(c2)
    with engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                    "AND database = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database())"
                )
            ).scalar_one()
            == 0
        )


def test_lock_loss_is_detected(migrated: str) -> None:
    """Simulates a DB-side disconnect of the lock holder (e.g. DB restart)."""
    from autotrader.db.market_data import (
        lock_still_held,
        open_lock_connection,
        release_collector_lock,
        try_collector_lock,
    )

    engine = create_engine(migrated)
    conn = open_lock_connection(migrated)
    try:
        assert try_collector_lock(conn)
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        with engine.connect() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        assert lock_still_held(conn) is False
    finally:
        release_collector_lock(conn)  # must not raise on a dead connection
