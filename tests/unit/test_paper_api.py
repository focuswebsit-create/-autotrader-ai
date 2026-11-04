"""Phase 3 read-only PAPER endpoints: numbers come from the worker's ledger, GET only."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from autotrader.api.app import create_app
from autotrader.common.config import Settings
from autotrader.db.market_data import write_heartbeat
from tests.unit.paper_helpers import book, tick
from tests.unit.test_paper_engine import _open_long, env  # noqa: F401  (fixture)


def _client(sessions: Any, now: Any) -> TestClient:
    return TestClient(create_app(Settings(market_coin="xyz:CL"), sessions, clock=lambda: now))


def _quote(sessions: Any, now: Any, bid: str, ask: str) -> None:
    b = book(now, bid, ask)
    with sessions.begin() as s:
        write_heartbeat(s, "paper-trader", state="RUNNING", ws_state="NOT_APPLICABLE",
                        started_at=now, last_heartbeat_at=now,
                        details={"book": {"time": now.isoformat(), "bid": str(b.best_bid),
                                          "ask": str(b.best_ask), "mid": str(b.mid),
                                          "spread_bps": "2.5"}})  # fmt: skip


def test_paper_routes_are_get_only(env: Any) -> None:  # noqa: F811
    sessions, *_ = env
    app = create_app(Settings(), sessions)
    paths = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith("/api/paper")}
    assert set(paths) >= {"/api/paper/summary", "/api/paper/trades", "/api/paper/decisions",
                          "/api/paper/events", "/api/paper/equity"}  # fmt: skip
    client = TestClient(app)
    for path, ops in paths.items():
        assert set(ops) <= {"get", "head"}
        for method in ("post", "put", "patch", "delete"):
            assert client.request(method, path).status_code == 405


def test_summary_open_position_and_flat_after_exit(env: Any) -> None:  # noqa: F811
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=2)
    _quote(sessions, t, "80.50", "80.52")
    r = _client(sessions, t).get("/api/paper/summary").json()
    assert r["paper_only"] is True and r["bot"]["state"] == "RUNNING"
    assert r["position"]["side"] == "LONG" and r["market"]["fresh"] is True
    assert Decimal(r["pnl"]["unrealized"]) > 0
    assert "experimental" in r["account"]["experimental_warning"].lower()

    exit_t = now + timedelta(seconds=30)
    tick(sessions, acc_id, exit_t, book(exit_t, "83.10", "83.12"))  # target hit
    c = _client(sessions, exit_t + timedelta(seconds=1))
    r = c.get("/api/paper/summary").json()
    assert r["position"] is None and r["stats"]["closed_trades"] == 1
    assert Decimal(r["pnl"]["realized_net"]) > 0
    assert Decimal(r["pnl"]["unrealized"]) == 0
    # equity = 1000 + net realized (no open position)
    assert Decimal(r["pnl"]["equity"]) == Decimal(1000) + Decimal(r["pnl"]["realized_net"])
    trades = c.get("/api/paper/trades?period=day").json()["trades"]
    assert len(trades) == 1 and trades[0]["side"] == "LONG"
    assert c.get("/api/paper/trades?period=all").json()["trades"] == trades
    decs = c.get("/api/paper/decisions").json()["decisions"]
    assert any(d["outcome"] == "ORDER" for d in decs)
    events = c.get("/api/paper/events").json()["events"]
    assert events and {"time", "type", "kind", "reason"} <= set(events[0])
    eq = c.get("/api/paper/equity").json()
    assert eq["initial_equity"] == "1000.00"


def test_stale_book_is_not_marked(env: Any) -> None:  # noqa: F811
    sessions, _, now = _open_long(env)
    _quote(sessions, now, "80.50", "80.52")
    later = now + timedelta(minutes=5)  # worker quote is 5 min old
    r = _client(sessions, later).get("/api/paper/summary").json()
    assert r["market"]["fresh"] is False
    assert r["position"]["unrealized_note"].startswith("PRICE_STALE")


def test_bad_inputs(env: Any) -> None:  # noqa: F811
    sessions, *_ = env
    c = TestClient(create_app(Settings(), sessions))
    assert c.get("/api/paper/summary?account=nope").status_code == 404
    assert c.get("/api/paper/summary?account=bad%20name").status_code == 422
    assert c.get("/api/paper/trades?period=year").status_code == 422
    assert c.get("/api/paper/decisions?limit=100000").status_code == 422
    nodb = TestClient(create_app(Settings(database_url=None)))
    assert nodb.get("/api/paper/summary").status_code == 503


def test_paper_health_for_monitoring(env: Any) -> None:  # noqa: F811
    from tests.unit.paper_helpers import command

    sessions, acc_id, _ = env
    from tests.unit.test_paper_engine import T0

    r = _client(sessions, T0).get("/api/paper/health")
    assert r.status_code == 503 and r.json()["alerts"][0]["code"] == "PAPER_WORKER_DOWN"
    _quote(sessions, T0, "80.00", "80.02")
    r = _client(sessions, T0 + timedelta(seconds=5)).get("/api/paper/health")
    assert r.status_code == 200 and r.json()["status"] == "OK"
    assert r.json()["accounts"] == {"demo": "STOPPED"}
    command(sessions, acc_id, "KILL", T0, "kill-health-1")
    tick(sessions, acc_id, T0, book(T0, "80.00", "80.02"))
    r = _client(sessions, T0 + timedelta(seconds=5)).get("/api/paper/health")
    assert r.status_code == 503 and r.json()["alerts"][0]["code"] == "PAPER_DEMO_HALTED:KILL_SWITCH"
    r = _client(sessions, T0 + timedelta(minutes=5)).get("/api/paper/health")
    assert {a["code"] for a in r.json()["alerts"]} >= {"PAPER_WORKER_DOWN"}


def test_paper_health_without_accounts() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from autotrader.db.models import Base

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)  # fmt: skip
    Base.metadata.create_all(eng)
    c = TestClient(create_app(Settings(), sessionmaker(bind=eng)))
    r = c.get("/api/paper/health")
    assert r.status_code == 200 and r.json()["status"] == "NOT_CONFIGURED"
