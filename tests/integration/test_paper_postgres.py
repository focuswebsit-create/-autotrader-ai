"""Phase 3 PAPER trading on real PostgreSQL: DB-enforced guards, roles, full flow.

Same safety rule as ``test_postgres.py``: only a database named ``*_test`` (the
fixture drops and recreates every table). Timestamps are REAL wall-clock times,
because the migration refuses paper rows stamped more than 5 minutes from
``clock_timestamp()`` (no retroactive trades).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.control import auth
from autotrader.control.app import create_control_app
from autotrader.db.models import (
    AuditLog,
    PaperBotEvent,
    PaperCommand,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperPositionClose,
)
from autotrader.paper import engine, worker
from autotrader.paper.config import PaperConfig, PaperExecution
from autotrader.strategy.base import Side
from tests.integration.test_postgres import DB_URL, migrated  # noqa: F401  (fixture)
from tests.unit.paper_helpers import (
    COIN,
    STEP,
    ScriptedStrategy,
    add_bars,
    book,
    collector_ok,
    command,
    funding,
)

pytestmark = [pytest.mark.db]
if not (DB_URL.startswith("postgresql") and str(make_url(DB_URL).database).endswith("_test")):
    pytestmark.append(pytest.mark.skip(reason="set AUTOTRADER_TEST_DATABASE_URL (*_test)"))

ROLE_PW = "test-only-pw"
SECRET = b"pg-test-control-secret-not-hex-0123456789-xyz"
ORIGIN = "http://localhost:3000"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _role_url(role: str) -> str:
    return make_url(DB_URL).set(username=role, password=ROLE_PW).render_as_string(False)


def _err(sessions: sessionmaker[Session], sql: str, params: dict[str, Any] | None = None) -> str:
    try:
        with sessions.begin() as s:
            s.execute(text(sql), params or {})
    except DBAPIError as exc:
        return str(exc.orig).lower()
    return ""


class World:
    def __init__(self, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sessions = sessionmaker(bind=create_engine(url), expire_on_commit=False)
        self.script: dict[datetime, Side] = {}
        self.t0 = _now()
        # the latest 30m bar closes 20 s from now; history before it (not hour-aligned:
        # the engine only needs close times; the 1h series ends with the same close)
        self.signal_open = self.t0 + timedelta(seconds=20) - STEP
        with self.sessions.begin() as s:
            add_bars(s, self.signal_open - 199 * STEP, 200)  # ends with the signal bar
            add_bars(s, self.signal_open + STEP - timedelta(hours=100), 100, interval="1h")
            hour = self.t0.replace(minute=0, second=0)
            for h in range(0, 3):
                funding(s, hour - timedelta(hours=h))
            collector_ok(s, self.t0)
            # this container runs at any hour: the CME session filter is a strategy
            # filter, not what is tested here
            cfg = PaperConfig(execution=PaperExecution(session_filter=False))
            self.acc_id = engine.create_account(s, name="demo", coin=COIN, now=self.t0,
                                                config=cfg).id  # fmt: skip
        monkeypatch.setattr(engine, "build_strategy", lambda _sid: ScriptedStrategy(self.script))

    def run(self, now: datetime, bid: str = "80.00", ask: str = "80.02") -> dict[str, Any]:
        with self.sessions.begin() as s:
            collector_ok(s, now)
        return worker.run_once(self.sessions, COIN, None, now, book=book(now, bid, ask))

    def state(self) -> PaperBotEvent:
        with self.sessions() as s:
            return engine.current_state(s, self.acc_id)

    def count(self, model: Any) -> int:
        with self.sessions() as s:
            return int(s.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def world(migrated: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:  # noqa: F811
    yield World(migrated, monkeypatch)


@pytest.fixture
def logins(migrated: str) -> dict[str, sessionmaker[Session]]:  # noqa: F811
    """Login users exactly as infra/db/roles.sql defines them."""
    eng = create_engine(migrated)
    with eng.begin() as c:
        for login, group in (("autotrader_api", "autotrader_reader"),
                             ("autotrader_worker", "autotrader_writer"),
                             ("autotrader_control", "autotrader_controller")):  # fmt: skip
            if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": login}).first():
                c.execute(text(f"CREATE ROLE {login} LOGIN"))
            c.execute(text(f"ALTER ROLE {login} WITH LOGIN NOSUPERUSER INHERIT "
                           f"PASSWORD '{ROLE_PW}'"))  # fmt: skip
            c.execute(text(f"GRANT {group} TO {login}"))
    return {
        k: sessionmaker(bind=create_engine(_role_url(r)), expire_on_commit=False)
        for k, r in (
            ("api", "autotrader_api"),
            ("worker", "autotrader_worker"),
            ("control", "autotrader_control"),
        )
    }


# --------------------------------------------------------------------------- full flow


def test_full_paper_round_trip_on_postgres(world: World) -> None:
    w = world
    start_at = w.t0
    command(w.sessions, w.acc_id, "START", start_at, "pg-start-0001")
    out = w.run(start_at)
    assert out["states"][str(w.acc_id)] == "RUNNING", w.state().detail
    w.script[w.signal_open] = Side.LONG

    entry_at = w.signal_open + STEP + timedelta(seconds=2)
    w.run(entry_at)
    w.run(entry_at + timedelta(seconds=1))  # a second tick on the same bar: no duplicate
    with w.sessions() as s:
        pos = engine.open_position(s, w.acc_id)
        assert pos is not None and pos.side == "LONG"
        assert pos.entry_price == Decimal("80.037")
    assert w.count(PaperPosition) == 1 and w.count(PaperOrder) == 1

    stop_at = entry_at + timedelta(seconds=10)
    w.run(stop_at, "78.40", "78.42")  # stop 78.537 crossed
    with w.sessions() as s:
        assert engine.open_position(s, w.acc_id) is None
        close = s.scalar(select(PaperPositionClose))
        assert close.reason == "STOP" and close.net_pnl < 0
        acc = s.get(engine.PaperAccount, w.acc_id)
        ok, detail = engine.ledger_reconciles(s, acc)
        assert ok, detail
        # exact NUMERIC accounting: cash = 1000 + ledger
        cash = engine.cash(s, w.acc_id)
        assert cash == Decimal(1000) + close.net_pnl - 0  # fees are inside net_pnl
    assert w.state().state == "RUNNING"


def test_db_refuses_entries_unless_running(world: World) -> None:
    w = world
    now = _now()
    sql = ("INSERT INTO paper_positions (account_id, decision_id, entry_order_id, side, size, "
           "entry_price, stop, target, risk_budget, planned_loss, opened_at, entry_fee) "
           "VALUES (:a, 1, 1, 'LONG', 1, 80, 79, 82, 2.5, 1, :t, 0)")  # fmt: skip
    msg = _err(w.sessions, sql, {"a": w.acc_id, "t": now})
    assert msg, "a position was inserted while the bot is STOPPED"


def test_db_refuses_a_second_open_position(world: World) -> None:
    w = world
    command(w.sessions, w.acc_id, "START", w.t0, "pg-start-0002")
    w.run(w.t0)
    w.script[w.signal_open] = Side.SHORT
    w.run(w.signal_open + STEP + timedelta(seconds=2))
    with w.sessions() as s:
        pos = engine.open_position(s, w.acc_id)
        assert pos is not None and pos.side == "SHORT"
        cols = {c.name for c in PaperPosition.__table__.columns} - {"id"}
        row = {c: getattr(pos, c) for c in cols}
    row["opened_at"] = _now()
    names = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    msg = _err(w.sessions, f"INSERT INTO paper_positions ({names}) VALUES ({vals})", row)
    assert msg and "position" in msg


def test_paper_history_is_append_only(world: World) -> None:
    w = world
    for sql in ("UPDATE paper_ledger SET amount = amount + 1",
                "DELETE FROM paper_ledger",
                "DELETE FROM paper_bot_events",
                "UPDATE paper_bot_events SET state = 'RUNNING'",
                "UPDATE paper_accounts SET initial_equity = 1000000",
                "TRUNCATE paper_ledger",
                "TRUNCATE paper_accounts CASCADE"):  # fmt: skip
        assert _err(w.sessions, sql), sql
    assert w.count(PaperLedger) == 1  # the deposit is still there


def test_retroactive_rows_are_refused(world: World) -> None:
    w = world
    old = _now() - timedelta(hours=2)
    msg = _err(w.sessions,
               "INSERT INTO paper_bot_events (account_id, state, reason, occurred_at, detail) "
               "VALUES (:a, 'RUNNING', 'FAKE', :t, '{}')", {"a": w.acc_id, "t": old})  # fmt: skip
    assert msg
    msg = _err(w.sessions,
               "INSERT INTO paper_ledger (account_id, kind, amount, ref, occurred_at, detail) "
               "VALUES (:a, 'DEPOSIT', 1000, 'fake:1', :t, '{}')",
               {"a": w.acc_id, "t": old})  # fmt: skip
    assert msg
    # a worker that wakes up after a long outage cannot backfill trades: the engine
    # only decides bars inside the entry window
    command(w.sessions, w.acc_id, "START", w.t0, "pg-start-0003")
    w.run(w.t0)
    assert w.state().state == "RUNNING"


def test_auth_sessions_can_only_be_revoked_once(world: World) -> None:
    w = world
    now = _now()
    with w.sessions.begin() as s:
        user = auth.create_user(s, "owner", "correct horse battery", "ADMIN", now)
        res = auth.login(s, "owner", "correct horse battery", auth.client_hash("1.2.3.4"),
                         SECRET, now)  # fmt: skip
        assert res is not None and user.id
    assert _err(w.sessions, "UPDATE auth_sessions SET expires_at = expires_at + interval '1 day'")
    assert _err(w.sessions, "DELETE FROM auth_sessions")
    assert not _err(w.sessions, "UPDATE auth_sessions SET revoked_at = now()")
    assert _err(w.sessions, "UPDATE auth_sessions SET revoked_at = NULL")


# --------------------------------------------------------------------------- roles


def test_controller_role_can_only_append_commands(
    world: World, logins: dict[str, sessionmaker[Session]]
) -> None:
    w, ctl = world, logins["control"]
    now = _now()
    assert not _err(ctl,
                    "INSERT INTO paper_commands (account_id, command, requested_by, reason, "
                    "idempotency_key, requested_at) VALUES (:a,'START','t','t','ctl-key-001',:t)",
                    {"a": w.acc_id, "t": now})  # fmt: skip
    for sql in (
        "INSERT INTO paper_ledger (account_id, kind, amount, ref, occurred_at, detail) "
        "VALUES (1, 'DEPOSIT', 5, 'x', now(), '{}')",
        "INSERT INTO paper_bot_events (account_id, state, reason, occurred_at, detail) "
        "VALUES (1, 'RUNNING', 'X', now(), '{}')",
        "UPDATE paper_commands SET command = 'KILL'",
        "DELETE FROM paper_commands",
        "UPDATE auth_users SET role = 'ADMIN'",
        "UPDATE auth_sessions SET expires_at = now()",
        "INSERT INTO candles (coin, interval, open_time, close_time, open, high, low, close, "
        "volume, is_closed, source, first_seen_at, updated_at) VALUES ('xyz:CL','30m', now(), "
        "now(), 1,1,1,1,1, true,'rest', now(), now())",
        "UPDATE system_config SET value='false'::jsonb WHERE key='paper_only'",
    ):
        assert _err(ctl, sql), sql


def test_worker_and_reader_roles(world: World, logins: dict[str, sessionmaker[Session]]) -> None:
    w = world
    assert _err(logins["worker"],
                "INSERT INTO paper_commands (account_id, command, requested_by, reason, "
                "idempotency_key, requested_at) VALUES (:a,'START','w','w','wrk-key-001',now())",
                {"a": w.acc_id})  # fmt: skip
    assert _err(logins["worker"], "INSERT INTO auth_users (username, password_hash, role, "
                "created_at) VALUES ('x','y','ADMIN', now())")  # fmt: skip
    assert _err(logins["api"], "INSERT INTO paper_commands (account_id, command, requested_by, "
                "reason, idempotency_key, requested_at) VALUES (1,'START','a','a','api-k-01',"
                "now())")  # fmt: skip
    with logins["api"]() as s:
        assert s.scalar(text("SELECT count(*) FROM paper_ledger")) == 1
    assert _err(logins["api"], "SELECT count(*) FROM auth_sessions")  # tokens hashes stay private


def test_auth_tables_are_private(migrated: str) -> None:  # noqa: F811
    eng = create_engine(migrated)
    with eng.connect() as c:
        for t in ("auth_users", "auth_sessions", "auth_login_attempts"):
            for role in ("autotrader_reader", "autotrader_writer"):
                assert not c.execute(text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                                     {"r": role, "t": t}).scalar(), (role, t)  # fmt: skip
            assert c.execute(text("SELECT has_table_privilege('autotrader_controller', :t, "
                                  "'SELECT')"), {"t": t}).scalar()  # fmt: skip
        for t in ("paper_commands", "paper_ledger", "paper_positions", "paper_bot_events"):
            for role in ("autotrader_reader", "autotrader_writer", "autotrader_controller"):
                assert c.execute(text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                                 {"r": role, "t": t}).scalar(), (role, t)  # fmt: skip


def test_control_api_and_worker_end_to_end_with_least_privilege_roles(
    world: World, logins: dict[str, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner logs in (controller role) -> START command -> worker (writer role) runs it."""
    w = world
    with w.sessions.begin() as s:
        auth.create_user(s, "owner", "correct horse battery", "ADMIN", _now())
    app = create_control_app(Settings(), logins["control"], secret=SECRET,
                             allowed_origins=(ORIGIN,), cookie_secure=False)  # fmt: skip
    client = TestClient(app)
    r = client.post("/control/login",
                    json={"username": "owner", "password": "correct horse battery"})  # fmt: skip
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    hdr = {"origin": ORIGIN, "x-csrf-token": csrf}
    r = client.post("/control/bot/start", json={"idempotency_key": "e2e-start-1"}, headers=hdr)
    assert r.status_code == 200, r.text
    r2 = client.post("/control/bot/start", json={"idempotency_key": "e2e-start-2"}, headers=hdr)
    assert r2.status_code == 409 and r2.json()["detail"] == "COMMAND_PENDING"

    now = _now()
    with w.sessions.begin() as s:
        collector_ok(s, now)
    out = worker.run_once(logins["worker"], COIN, None, now, book=book(now, "80.00", "80.02"))
    assert out["states"][str(w.acc_id)] == "RUNNING", w.state().detail
    r3 = client.post("/control/bot/start", json={"idempotency_key": "e2e-start-3"}, headers=hdr)
    assert r3.status_code == 409 and r3.json()["detail"] == "NOT_ALLOWED_FROM_RUNNING"
    r4 = client.post("/control/bot/kill", json={"idempotency_key": "e2e-kill-01"}, headers=hdr)
    assert r4.status_code == 200
    worker.run_once(logins["worker"], COIN, None, _now(), book=book(_now(), "80.00", "80.02"))
    assert w.state().state == "HALTED" and w.state().reason == "KILL_SWITCH"
    with w.sessions() as s:
        actions = s.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
        assert actions == ["LOGIN", "PAPER_START", "PAPER_KILL"]
        assert s.scalar(select(func.count()).select_from(PaperCommand)) == 2
