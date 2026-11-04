"""Alembic migrations + DB guards on real PostgreSQL.

These tests DROP AND RECREATE all tables. They therefore use a dedicated variable,
``AUTOTRADER_TEST_DATABASE_URL``, and refuse any database whose name does not end
in ``_test`` (review finding M4), so they can never wipe a dev/prod database.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, select, text
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
        # A FORMING bar (is_closed=false) may be updated by the collector.
        c.execute(
            text(CANDLE_SQL.format(o=90, h=91, l=89, c=90.5).replace("true,'rest'", "false,'ws'"))
        )
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


def test_closed_candle_is_immutable_in_db(roles: dict[str, Any]) -> None:
    """Final review L1: immutability of closed bars is enforced by the DB (migration 0004)."""
    w = roles["worker"]
    with w.begin() as c:
        c.execute(text(CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)))  # closed bar
    with pytest.raises(DBAPIError, match="immutable"), w.begin() as c:
        c.execute(text("UPDATE candles SET close = 90.9 WHERE is_closed"))
    with pytest.raises(DBAPIError, match="immutable"), w.begin() as c:
        c.execute(text("UPDATE candles SET is_closed = false"))
    # A forming bar can still be updated and then closed exactly once.
    with w.begin() as c:
        c.execute(
            text(
                CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)
                .replace("true,'rest'", "false,'ws'")
                .replace(
                    "'2026-01-01T00:00:00Z','2026-01-01T00:30:00Z'",
                    "'2026-01-01T00:30:00Z','2026-01-01T01:00:00Z'",
                )
            )
        )
        c.execute(text("UPDATE candles SET volume = 5 WHERE NOT is_closed"))
        c.execute(text("UPDATE candles SET is_closed = true, source = 'rest' WHERE NOT is_closed"))
    with pytest.raises(DBAPIError, match="immutable"), w.begin() as c:
        c.execute(text("UPDATE candles SET volume = 6"))


def test_cli_backfill_refuses_while_collector_holds_lock(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review M4: the CLI backfill respects the single-writer lock."""
    from autotrader import cli
    from autotrader.common.config import get_settings
    from autotrader.db.market_data import (
        open_lock_connection,
        release_collector_lock,
        try_collector_lock,
    )

    holder = open_lock_connection(migrated)
    try:
        assert try_collector_lock(holder)
        monkeypatch.setenv("AUTOTRADER_DATABASE_URL", migrated)
        get_settings.cache_clear()

        class NoNetwork:
            def __init__(self, *a: Any, **k: Any) -> None:
                raise AssertionError("backfill must not fetch while the lock is held")

        monkeypatch.setattr(cli, "InfoClient", NoNetwork)
        assert cli.main(["backfill"]) == 5
        assert "COLLECTOR_RUNNING" in capsys.readouterr().err
    finally:
        release_collector_lock(holder)
        get_settings.cache_clear()


# ------------------------------------------------------------ controlled correction (0005)

CLOSED_BAR = CANDLE_SQL.format(o=90, h=91, l=89, c=90.5)


def test_correction_requires_ticket_and_non_service_role(
    roles: dict[str, Any], migrated: str
) -> None:
    owner = create_engine(migrated)
    with owner.begin() as c:
        c.execute(text(CLOSED_BAR))
    # Owner without a ticket: still immutable.
    with pytest.raises(DBAPIError, match="immutable"), owner.begin() as c:
        c.execute(text("UPDATE candles SET close = 90.4"))
    # The collector's role cannot use the path even if it sets a ticket.
    with pytest.raises(DBAPIError, match="immutable"), roles["worker"].begin() as c:
        c.execute(text("SELECT set_config('autotrader.correction_ticket', 'X-1', true)"))
        c.execute(text("UPDATE candles SET close = 90.4"))
    # Owner with a ticket: allowed, and audited by the trigger itself.
    with owner.begin() as c:
        c.execute(text("SELECT set_config('autotrader.correction_ticket', 'QA-42', true)"))
        c.execute(text("UPDATE candles SET close = 90.4"))
    with owner.connect() as c:
        row = c.execute(
            text("SELECT actor, action, before->>'close', after->>'close' FROM audit_log")
        ).one()
        assert row[1] == "CORRECT_CLOSED_CANDLE ticket=QA-42"
        assert Decimal(row[2]) == Decimal("90.5") and Decimal(row[3]) == Decimal("90.4")
    # The ticket is transaction-local: the next transaction is protected again.
    with pytest.raises(DBAPIError, match="immutable"), owner.begin() as c:
        c.execute(text("UPDATE candles SET close = 90.3"))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE candles SET is_closed = false",
        "UPDATE candles SET open_time = open_time + interval '30 minutes', "
        "close_time = close_time + interval '30 minutes'",
        "UPDATE candles SET interval = '1h'",
    ],
)
def test_correction_cannot_change_identity_or_reopen(migrated: str, sql: str) -> None:
    owner = create_engine(migrated)
    with owner.begin() as c:
        c.execute(text(CLOSED_BAR))
    with pytest.raises(DBAPIError, match="identity or reopen"), owner.begin() as c:
        c.execute(text("SELECT set_config('autotrader.correction_ticket', 'QA-1', true)"))
        c.execute(text(sql))


def test_repair_flow_backup_audit_and_idempotent(
    migrated: str, snapshot_30m: Any, tmp_path: Any
) -> None:
    from autotrader.ingest.repair import apply_corrections, find_differences
    from autotrader.ingest.rest import backfill
    from autotrader.market.candles import Interval
    from tests.conftest import Clock

    class Info:
        def info(self, payload: dict[str, Any]) -> Any:
            return snapshot_30m

    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    assert backfill(sessions, Info(), coin="xyz:CL", interval=Interval.M30, clock=Clock()).ok
    # Simulate a bar frozen from partial data (e.g. the pre-H1 bug).
    target = snapshot_30m[150]
    with sessions.begin() as s:
        s.execute(text("SELECT set_config('autotrader.correction_ticket', 'SIMULATE', true)"))
        s.execute(
            text(
                "UPDATE candles SET volume = volume / 2 WHERE open_time = to_timestamp(:t / 1000.0)"
            ),
            {"t": target["t"]},
        )
    with sessions() as s:
        diffs, exchange, compared = find_differences(s, Info(), "xyz:CL", Interval.M30)
    assert compared == 200 and len(diffs) == 1  # dry run only reports
    assert diffs[0].exchange["volume"] == target["v"]

    backup = apply_corrections(
        sessions, "xyz:CL", Interval.M30, diffs, exchange, ticket="QA-7", backup_dir=tmp_path
    )
    saved = json.loads(backup.read_text())
    assert saved["ticket"] == "QA-7" and saved["rows"][0]["stored"]["volume"] != target["v"]
    with sessions() as s:
        assert find_differences(s, Info(), "xyz:CL", Interval.M30)[0] == []  # fixed, idempotent
        actions = s.execute(text("SELECT action FROM audit_log ORDER BY id")).scalars().all()
        assert actions == [
            "CORRECT_CLOSED_CANDLE ticket=SIMULATE",
            "CORRECT_CLOSED_CANDLE ticket=QA-7",
        ]
        issue = s.execute(
            text("SELECT detail FROM data_quality_issues WHERE issue_type='REVISED_CLOSED_CANDLE'")
        ).scalar_one()
        assert "QA-7" in issue


def test_apply_requires_ticket(migrated: str, tmp_path: Any) -> None:
    from autotrader.ingest.repair import apply_corrections
    from autotrader.market.candles import Interval

    sessions = sessionmaker(bind=create_engine(migrated))
    with pytest.raises(ValueError, match="ticket"):
        apply_corrections(sessions, "xyz:CL", Interval.M30, [], {}, ticket=" ", backup_dir=tmp_path)


# ------------------------------------------------------------ Phase 2: backtest storage (0006)


def test_backtest_runs_are_immutable_and_roundtrip(migrated: str, recorded_market: Any) -> None:
    from datetime import UTC, datetime

    from autotrader.backtest.research import evaluate_strategy
    from autotrader.backtest.store import get_run, save_run

    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    report = evaluate_strategy(recorded_market, "breakout_volume", code_version="test")
    with sessions.begin() as s:
        run_id = save_run(s, report, now=datetime.now(UTC)).id
    with sessions() as s:
        row = get_run(s, run_id)
        assert row is not None and row.report["report_sha256"] == report["report_sha256"]
        assert row.verdict == report["verdict"]["status"]
    engine = create_engine(migrated)
    for sql in (
        "UPDATE backtest_runs SET verdict='PASS'",
        "DELETE FROM backtest_trades",
        "DELETE FROM backtest_runs",
    ):
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(sql))


def test_funding_rates_append_only(migrated: str) -> None:
    from datetime import UTC, datetime

    engine = create_engine(migrated)
    now = datetime.now(UTC)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO funding_rates (coin, time, hour, rate, source, received_at) "
                "VALUES ('xyz:CL', :t, :t, 0.0001, 'test', :t)"
            ),
            {"t": now},
        )
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE funding_rates SET rate = 0"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO funding_rates (coin, time, hour, rate, source, received_at) "
                "VALUES ('xyz:CL', :t, :t, 0.0001, 'dup', :t)"
            ),
            {"t": now},
        )


# ------------------------------------------------------------ Phase 2.5: forward tests (0007)


def _forward_test_row(c: Any, starts: str = "2026-11-05T00:00:00Z") -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO forward_tests (coin, strategy_id, strategy_version, params_hash, "
                "code_hash, code_version, config, config_sha256, history_anchor, starts_at, "
                "created_at) VALUES ('xyz:CL','ema_rsi','1.0.0',:h,:h,'t','{}'::jsonb,:h, "
                "'2026-07-23T00:00:00Z', :s, '2026-11-04T23:50:00Z') RETURNING id"
            ),
            {"h": "a" * 64, "s": starts},
        ).scalar_one()
    )


def _without_wallclock_guard(c: Any) -> None:
    """Owner-only: lets a test insert fixed historical timestamps (the guard is tested
    separately in test_forward_journal_accepts_only_real_time_writes)."""
    for table in ("forward_tests", "forward_decisions"):
        c.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_wallclock"))


def test_forward_journal_is_immutable_and_starts_after_freeze(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        _without_wallclock_guard(c)
        tid = _forward_test_row(c)
        c.execute(
            text(
                "INSERT INTO forward_decisions (test_id, bar_open_time, bar_close_time, "
                "evaluated_at, late, side, reason, rationale, market, strategy_version, "
                "params_hash, code_hash, input_digest) VALUES (:t, '2026-11-05T00:00:00Z', "
                "'2026-11-05T00:30:00Z', '2026-11-05T00:30:10Z', false, NULL, 'NO_SETUP', "
                "'{}'::jsonb, '{}'::jsonb, '1.0.0', :h, :h, :h)"
            ),
            {"t": tid, "h": "a" * 64},
        )
    bad = {
        # decision for a bar BEFORE the test started (backfilling the past)
        "before_start": (
            "INSERT INTO forward_decisions (test_id, bar_open_time, bar_close_time, evaluated_at, "
            "late, reason, rationale, market, strategy_version, params_hash, code_hash, "
            "input_digest) VALUES (:t, '2026-11-04T23:30:00Z', '2026-11-05T00:00:00Z', "
            "'2026-11-05T00:01:00Z', false, 'NO_SETUP', '{}', '{}', '1', :h, :h, :h)"
        ),
        # decided before the bar closed (look-ahead)
        "lookahead": (
            "INSERT INTO forward_decisions (test_id, bar_open_time, bar_close_time, evaluated_at, "
            "late, reason, rationale, market, strategy_version, params_hash, code_hash, "
            "input_digest) VALUES (:t, '2026-11-05T01:00:00Z', '2026-11-05T01:30:00Z', "
            "'2026-11-05T01:29:00Z', false, 'NO_SETUP', '{}', '{}', '1', :h, :h, :h)"
        ),
        "duplicate_bar": (
            "INSERT INTO forward_decisions (test_id, bar_open_time, bar_close_time, evaluated_at, "
            "late, reason, rationale, market, strategy_version, params_hash, code_hash, "
            "input_digest) VALUES (:t, '2026-11-05T00:00:00Z', '2026-11-05T00:30:00Z', "
            "'2026-11-05T00:31:00Z', false, 'NO_SETUP', '{}', '{}', '1', :h, :h, :h)"
        ),
        "edit_decision": "UPDATE forward_decisions SET side = 'LONG'",
        "delete_decision": "DELETE FROM forward_decisions",
        "edit_test": "UPDATE forward_tests SET config = '{\"fast\": 1}'::jsonb",
        "delete_test": "DELETE FROM forward_tests",
    }
    for name, sql in bad.items():
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(sql), {"t": tid, "h": "a" * 64})
            pytest.fail(name)  # pragma: no cover
    # a test can't start in the past relative to its creation
    with pytest.raises(DBAPIError), engine.begin() as c:
        _forward_test_row(c, starts="2026-11-04T20:00:00Z")


def test_forward_journal_accepts_only_real_time_writes(migrated: str) -> None:
    """A historical replay can't be written into the forward journal: tests and decisions
    must carry the real current time (+-5 min of the DB clock)."""
    engine = create_engine(migrated)
    insert_test = (
        "INSERT INTO forward_tests (coin, strategy_id, strategy_version, params_hash, code_hash, "
        "code_version, config, config_sha256, history_anchor, starts_at, created_at) VALUES "
        "('xyz:CL','ema_rsi','1.0.0',:h,:h,'t','{}'::jsonb,:h,'2026-07-23T00:00:00Z', "
        ":created, :created) RETURNING id"
    )
    with pytest.raises(DBAPIError, match="real time"), engine.begin() as c:
        c.execute(text(insert_test), {"h": "a" * 64, "created": "2026-10-01T00:00:00Z"})
    with engine.begin() as c:
        now = c.execute(text("SELECT now() - interval '1 minute'")).scalar_one()
        start = c.execute(text("SELECT now() - interval '50 minutes'")).scalar_one()
        # freeze "now" (starts_at is not checked by this guard, created_at is)
        c.execute(text("ALTER TABLE forward_tests DISABLE TRIGGER trg_forward_tests_wallclock"))
        tid = c.execute(
            text(insert_test.replace(":created, :created", ":start, :start")),
            {"h": "a" * 64, "start": start},
        ).scalar_one()
        c.execute(text("ALTER TABLE forward_tests ENABLE TRIGGER trg_forward_tests_wallclock"))
    insert_decision = (
        "INSERT INTO forward_decisions (test_id, bar_open_time, bar_close_time, evaluated_at, "
        "late, reason, rationale, market, strategy_version, params_hash, code_hash, "
        "input_digest) VALUES (:t, :o, :c, :e, false, 'NO_SETUP', '{}', '{}', '1', :h, :h, :h)"
    )
    bar_open = start
    bar_close = c_close = start + __import__("datetime").timedelta(minutes=30)
    # replayed decision: evaluated "back then" -> rejected
    with pytest.raises(DBAPIError, match="real time"), engine.begin() as c:
        c.execute(text(insert_decision), {"t": tid, "o": bar_open, "c": bar_close,
                                          "e": c_close, "h": "a" * 64})  # fmt: skip
    # the same decision made now (late catch-up after an outage) -> accepted
    with engine.begin() as c:
        c.execute(text(insert_decision), {"t": tid, "o": bar_open, "c": bar_close, "e": now,
                                          "h": "a" * 64})  # fmt: skip


def test_forward_and_backtest_tables_cannot_be_truncated(migrated: str) -> None:
    engine = create_engine(migrated)
    for table in ("forward_tests", "forward_decisions", "forward_events", "funding_rates",
                  "backtest_runs", "backtest_trades"):  # fmt: skip
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(f"TRUNCATE {table} CASCADE"))


def test_concurrent_freeze_creates_one_active_test(migrated: str) -> None:
    """Review M3: two simultaneous freezes for one strategy -> exactly one active test."""
    import threading
    from datetime import UTC, datetime

    from autotrader.forward.recorder import active_tests, create_test

    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO candles (coin, interval, open_time, close_time, open, high, low, "
                "close, volume, is_closed, source, first_seen_at, updated_at) VALUES "
                "('xyz:CL','30m','2026-07-23T04:00:00Z','2026-07-23T04:30:00Z',90,91,89,90,1,"
                "true,'rest',now(),now())"
            )
        )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def freeze() -> None:
        barrier.wait()
        try:
            with sessions.begin() as s:
                create_test(s, "xyz:CL", "ema_rsi", now=datetime.now(UTC), code_version="t")
        except ValueError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=freeze) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with sessions() as s:
        assert len(active_tests(s, "xyz:CL")) == 1
    assert len(errors) == 1 and "already exists" in errors[0]


def test_forward_replay_parity_on_postgresql(migrated: str) -> None:
    """End-to-end forward recording + evaluation on PostgreSQL (not SQLite): stored
    NUMERIC values must reproduce exactly. Regression for the live run, where the
    first SIGNAL made a test INVALID because PostgreSQL rounded the stored ATR."""
    import gzip
    from datetime import UTC, datetime, timedelta

    from autotrader.db.market_data import upsert_candles
    from autotrader.db.models import ForwardDecision, ForwardTest
    from autotrader.forward import evaluate as ev
    from autotrader.forward import recorder as rec
    from autotrader.market.candles import Candle, Interval, parse_candle
    from tests.conftest import FIXTURES, PHASE2_RECORDED_AT

    engine = create_engine(migrated)
    with engine.begin() as c:
        # historical replay needs fixed timestamps: owner-only, test database only
        for table in ("forward_tests", "forward_decisions"):
            c.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER trg_{table}_wallclock"))
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for name, iv in (
        ("candleSnapshot_xyzCL_30m_2026-11-04_full.json.gz", Interval.M30),
        ("candleSnapshot_xyzCL_1h_2026-11-04_full.json.gz", Interval.H1),
    ):
        with gzip.open(FIXTURES / name, "rt") as f:
            raw = json.load(f)
        cs = [parse_candle(r, coin="xyz:CL", interval=iv) for r in raw]
        ok = [c for c in cs if isinstance(c, Candle) and c.is_closed_at(PHASE2_RECORDED_AT)]
        with sessions.begin() as s:
            upsert_candles(s, ok, source="rest", now=PHASE2_RECORDED_AT)
    freeze_at = datetime(2026, 10, 10, 9, 7, tzinfo=UTC)
    with sessions.begin() as s:
        tid = rec.create_test(s, "xyz:CL", "breakout_volume", now=freeze_at, code_version="t").id
    now = freeze_at
    while now < freeze_at + timedelta(days=4):
        now += timedelta(hours=6)
        with sessions.begin() as s:
            rec.tick(s, s.get(ForwardTest, tid), now=now)
    with sessions() as s:
        signals = [r for r in s.scalars(select(ForwardDecision)) if r.side]
        report = ev.evaluate(s, s.get(ForwardTest, tid), now=now)
    assert signals, "the replay window must contain at least one SIGNAL"
    assert report["parity"]["mismatches"] == 0, report["parity"]
    assert report["verdict"]["status"] == "INCONCLUSIVE"
