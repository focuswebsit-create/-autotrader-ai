"""Funding ingestion (strict parsing, paging, idempotency) and the read-only backtest API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from autotrader.api.app import create_app
from autotrader.backtest.store import load_market_data, save_run
from autotrader.common.config import Settings
from autotrader.db.models import BacktestTrade, FundingRate
from autotrader.ingest.funding import PAGE_LIMIT, fetch_funding, parse_funding, sync_funding
from tests.conftest import Clock

T = datetime(2026, 10, 1, tzinfo=UTC)


def _rec(h: int, rate: str = "0.0000125", coin: str = "xyz:CL") -> dict[str, Any]:
    ms = int((T + timedelta(hours=h)).timestamp() * 1000) + 37
    return {"coin": coin, "fundingRate": rate, "premium": "-0.0001", "time": ms}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"coin": "CL", "fundingRate": "0.1", "time": 1},
        {"coin": "xyz:CL", "fundingRate": "NaN", "time": 1},
        {"coin": "xyz:CL", "fundingRate": "1.5", "time": 1},
        {"coin": "xyz:CL", "fundingRate": "0.1", "time": True},
        {"coin": "xyz:CL", "fundingRate": "abc", "time": 5},
        {"coin": "xyz:CL", "time": 5},
    ],
)
def test_parse_funding_rejects_malformed(raw: Any) -> None:
    assert parse_funding(raw, "xyz:CL") is None


def test_parse_funding_floors_to_hour() -> None:
    r = parse_funding(_rec(3), "xyz:CL")
    assert r is not None and r.hour == T + timedelta(hours=3) and r.rate == Decimal("0.0000125")


class PagedInfo:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.calls: list[dict[str, Any]] = []

    def info(self, payload: dict[str, Any]) -> Any:
        assert payload["type"] == "fundingHistory"
        self.calls.append(payload)
        start = payload["startTime"]
        return [r for r in self.records if r["time"] >= start][:PAGE_LIMIT]


def test_fetch_pages_forward_and_dedupes() -> None:
    bad = _rec(1100) | {"fundingRate": "not-a-number"}
    recs = sorted([_rec(h) for h in range(1200)] + [_rec(5), bad], key=lambda r: r["time"])
    src = PagedInfo(recs)
    out, rejected, pages = fetch_funding(src, "xyz:CL", T)
    assert len(out) == 1200 and pages == 3 and rejected == 1
    assert [r.hour for r in out] == sorted(r.hour for r in out)


def test_sync_is_incremental_and_idempotent(sqlite_sessions: sessionmaker[Session]) -> None:
    src = PagedInfo([_rec(h) for h in range(50)])
    now = T + timedelta(days=3)
    with sqlite_sessions.begin() as s:
        assert sync_funding(s, src, "xyz:CL", start=T, now=now).inserted == 50
    with sqlite_sessions.begin() as s:
        assert sync_funding(s, src, "xyz:CL", start=T, now=now).inserted == 0
    src.records.append(_rec(50))
    with sqlite_sessions.begin() as s:
        r = sync_funding(s, src, "xyz:CL", start=T, now=now)
        assert r.inserted == 1
        assert s.scalar(select(func.count()).select_from(FundingRate)) == 51
    with sqlite_sessions() as s:
        data = load_market_data(s, "xyz:CL", as_of=now)
        assert len(data.funding) == 51 and data.m30 == []
        # as_of excludes funding not yet published at that time
        early = load_market_data(s, "xyz:CL", as_of=T + timedelta(hours=10))
        assert len(early.funding) == 10


def _report() -> dict[str, Any]:
    t = {
        "id": 1,
        "side": "LONG",
        "signal_time": "2026-10-13T14:30:00+00:00",
        "entry_time": "2026-10-13T14:30:00+00:00",
        "entry_price": "80.048",
        "exit_time": "2026-10-13T15:15:00+00:00",
        "exit_price": "78.5",
        "exit_reason": "STOP",
        "size": "1.426",
        "net_pnl": "-2.4",
        "r_multiple": "-0.96",
    }
    return {
        "strategy": {"id": "breakout_volume", "version": "1.0.0", "title_he": "x"},
        "params_hash": "a" * 64,
        "params": {"lookback": 20},
        "code": {"commit": "abc"},
        "data": {"coin": "xyz:CL", "fingerprint_sha256": "b" * 64},
        "trade_window": {"start": "2026-07-24T00:00:00+00:00", "end": "2026-11-04T07:30:00+00:00"},
        "verdict": {"status": "INCONCLUSIVE", "reasons": ["few trades"]},
        "primary": {"summary": {"trades": 1, "net_pnl": "-2.4"}, "trades": [t]},
        "report_sha256": "c" * 64,
    }


def test_backtest_api_lists_and_returns_stored_runs(sqlite_sessions: sessionmaker[Session]) -> None:
    with sqlite_sessions.begin() as s:
        run = save_run(s, _report(), now=T)
        run_id = run.id
        assert s.scalar(select(func.count()).select_from(BacktestTrade)) == 1
    c = TestClient(create_app(Settings(), sqlite_sessions, clock=Clock()))
    runs = c.get("/api/backtest/runs").json()["runs"]
    assert [r["id"] for r in runs] == [run_id] and runs[0]["verdict"] == "INCONCLUSIVE"
    body = c.get(f"/api/backtest/runs/{run_id}").json()
    assert body["report"]["primary"]["trades"][0]["exit_reason"] == "STOP"
    assert c.get("/api/backtest/runs/999").status_code == 404
    assert c.get("/api/backtest/runs/0").status_code == 422
    assert c.post("/api/backtest/runs").status_code == 405
    strategies = c.get("/api/backtest/strategies").json()["strategies"]
    assert {s["id"] for s in strategies} == {"ema_rsi", "trend_pullback", "breakout_volume"}


def test_backtest_api_without_db() -> None:
    c = TestClient(create_app(Settings(), None, clock=Clock()))
    assert c.get("/api/backtest/runs").json()["detail"] == "DATABASE_NOT_CONFIGURED"
