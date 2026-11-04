"""Market-data API: closed vs forming bars, look-ahead, alerts derived from real state."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from autotrader.api.app import create_app
from autotrader.api.market_data import expected_last_closed_open, num
from autotrader.common.config import Settings
from autotrader.db.market_data import write_heartbeat
from autotrader.ingest.live import SERVICE_NAME
from autotrader.ingest.rest import backfill
from autotrader.market.candles import Interval
from tests.conftest import RECORDED_AT, Clock


class StaticInfo:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def info(self, payload: dict[str, Any]) -> Any:
        return self.data[payload["req"]["interval"]]


@pytest.fixture
def loaded(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any, snapshot_1h: Any
) -> sessionmaker[Session]:
    info = StaticInfo({"30m": snapshot_30m, "1h": snapshot_1h})
    for iv in Interval:
        assert backfill(sqlite_sessions, info, coin="xyz:CL", interval=iv, clock=Clock()).ok
    return sqlite_sessions


def client(sessions: sessionmaker[Session], clock: Clock | None = None) -> TestClient:
    return TestClient(create_app(Settings(), sessions, clock=clock or Clock()))


def test_candles_returns_closed_bars_and_forming_separately(loaded: sessionmaker[Session]) -> None:
    body = client(loaded).get("/api/candles", params={"interval": "30m", "limit": 5000}).json()
    assert body["count"] == 200 and body["truncated"] is False
    assert all(c["closed"] for c in body["candles"])
    assert body["forming"]["closed"] is False
    assert body["forming"]["open_time"] == "2026-11-03T15:30:00+00:00"
    times = [c["t"] for c in body["candles"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert body["candles"][0]["c"] == num(__import__("decimal").Decimal(body["candles"][0]["c"]))


def test_candles_respect_as_of_clock_no_lookahead(loaded: sessionmaker[Session]) -> None:
    # API "now" one day earlier than the data: nothing that closed later is visible.
    early = Clock(RECORDED_AT - timedelta(days=1))
    body = (
        client(loaded, early).get("/api/candles", params={"interval": "30m", "limit": 5000}).json()
    )
    assert body["count"] == 200 - 48
    last_close = body["candles"][-1]["close_time"]
    assert last_close <= early.now.isoformat()


def test_candles_date_range_and_validation(loaded: sessionmaker[Session]) -> None:
    c = client(loaded)
    body = c.get(
        "/api/candles",
        params={"interval": "1h", "start": "2026-11-02T00:00:00Z", "end": "2026-11-03T00:00:00Z"},
    ).json()
    assert body["count"] == 24
    assert body["forming"] is None  # forming bar is outside the requested range
    assert c.get("/api/candles", params={"interval": "5m"}).status_code == 422
    assert c.get("/api/candles", params={"interval": "1h", "limit": 0}).status_code == 422
    assert c.get("/api/candles", params={"interval": "1h", "limit": 5001}).status_code == 422
    r = c.get(
        "/api/candles",
        params={"interval": "1h", "start": "2026-11-03T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
    )
    assert r.status_code == 422 and r.json()["detail"] == "START_AFTER_END"


def test_new_routes_are_get_only(loaded: sessionmaker[Session]) -> None:
    c = client(loaded)
    for path in (
        "/api/candles?interval=30m",
        "/api/data/report",
        "/api/data/issues",
        "/api/collector/status",
    ):
        assert c.post(path).status_code == 405
        assert c.delete(path).status_code == 405


def test_status_without_collector(loaded: sessionmaker[Session]) -> None:
    body = client(loaded).get("/api/collector/status").json()
    assert body["service_state"] == "NOT_STARTED"
    assert {"code": "COLLECTOR_NOT_STARTED", "severity": "ERROR"} in body["alerts"]
    assert body["overall"] == "ERROR"
    by_iv = {i["interval"]: i for i in body["intervals"]}
    assert by_iv["30m"]["closed_candles"] == 200 and by_iv["30m"]["fresh"] is True


def _hb(sessions: sessionmaker[Session], at: Any, **over: Any) -> None:
    fields: dict[str, Any] = dict(
        state="RUNNING",
        ws_state="CONNECTED",
        started_at=at,
        last_heartbeat_at=at,
        last_ws_message_at=at,
        last_candle_update_at=at,
        reconnects=0,
        details={"last_sync_ok": True, "market_ctx": {"markPx": "96.2"}},
    )
    fields.update(over)
    with sessions.begin() as s:
        write_heartbeat(s, SERVICE_NAME, **fields)


def test_status_healthy(loaded: sessionmaker[Session]) -> None:
    _hb(loaded, RECORDED_AT)
    body = client(loaded).get("/api/collector/status").json()
    assert body["service_state"] == "RUNNING" and body["overall"] == "OK" and body["alerts"] == []
    assert body["heartbeat"]["market_ctx"]["markPx"] == "96.2"


def test_status_detects_dead_collector(loaded: sessionmaker[Session]) -> None:
    _hb(loaded, RECORDED_AT - timedelta(minutes=5))  # process died without saying so
    body = client(loaded).get("/api/collector/status").json()
    assert body["service_state"] == "DOWN"
    assert any(a["code"] == "COLLECTOR_DOWN" for a in body["alerts"])


def test_status_detects_ws_problems_and_failed_sync(loaded: sessionmaker[Session]) -> None:
    _hb(
        loaded,
        RECORDED_AT,
        last_ws_message_at=RECORDED_AT - timedelta(minutes=3),
        details={"last_sync_ok": False, "last_error": "API_UNAVAILABLE"},
    )
    codes = {a["code"] for a in client(loaded).get("/api/collector/status").json()["alerts"]}
    assert {"WS_STALE", "REST_SYNC_FAILED"} <= codes
    _hb(loaded, RECORDED_AT, ws_state="DISCONNECTED")
    codes = {a["code"] for a in client(loaded).get("/api/collector/status").json()["alerts"]}
    assert "WS_DISCONNECTED" in codes


def test_status_detects_stale_data(loaded: sessionmaker[Session]) -> None:
    later = Clock(RECORDED_AT + timedelta(hours=2))
    _hb(loaded, later.now)
    body = client(loaded, later).get("/api/collector/status").json()
    stale = [a for a in body["alerts"] if a["code"] == "DATA_STALE"]
    assert {a["interval"] for a in stale} == {"30m", "1h"}


def test_expected_last_closed_open() -> None:
    assert (
        expected_last_closed_open(Interval.M30, RECORDED_AT).isoformat()
        == "2026-11-03T15:00:00+00:00"
    )
    assert (
        expected_last_closed_open(Interval.H1, RECORDED_AT).isoformat()
        == "2026-11-03T14:00:00+00:00"
    )


def test_report_and_issues(loaded: sessionmaker[Session]) -> None:
    c = client(loaded)
    report = c.get("/api/data/report").json()
    assert [i["closed_candles"] for i in report["intervals"]] == [200, 200]
    assert "5,000" in report["history_limit_note"]
    assert c.get("/api/data/issues").json() == {"issues": []}


def test_market_endpoints_503_when_db_down() -> None:
    dead = sessionmaker(
        bind=create_engine("postgresql+psycopg://u:p@127.0.0.1:1/x?connect_timeout=1")
    )
    c = TestClient(create_app(Settings(), dead))
    for path in (
        "/api/candles?interval=30m",
        "/api/data/report",
        "/api/data/issues",
        "/api/collector/status",
    ):
        r = c.get(path)
        assert r.status_code == 503 and r.json()["detail"] == "DATABASE_UNAVAILABLE", path


def test_num_formatting() -> None:
    from decimal import Decimal

    assert num(Decimal("94.595000000000000000")) == "94.595"
    assert num(Decimal("100.000000000000000000")) == "100"
    assert num(Decimal("0E-18")) == "0"
    assert num(Decimal("0.000000010000000000")) == "0.00000001"
