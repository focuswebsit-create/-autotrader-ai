"""Regression tests for the Phase 1 final independent review (H1, M1-M4, L2-L5, L8)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.api.app import create_app
from autotrader.common.config import Settings
from autotrader.common.safety import (
    INFO_URL,
    SafetyViolationError,
    check_request_allowed,
    check_ws_message,
)
from autotrader.db.market_data import list_issues, record_rejections, upsert_candles
from autotrader.db.models import CandleRow, IngestionRun
from autotrader.ingest.rest import ingest_rest
from autotrader.market.candles import Candle, CandleRejection, Interval, Rejected, parse_candle
from tests.conftest import RECORDED_AT, Clock
from tests.unit.test_live_collector import HANG, Connector, FakeConn, SnapshotInfo, make, run_until

COIN = "xyz:CL"
IV = Interval.M30


def _bars(raw: list[Any]) -> list[Candle]:
    out = [parse_candle(r, coin=COIN, interval=IV) for r in raw]
    return [c for c in out if isinstance(c, Candle)]


# --------------------------------------------------------------------------- H1


def test_h1_slow_response_does_not_close_a_bar_that_was_open_when_requested(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    sent_at = datetime(2026, 11, 3, 15, 59, 59, tzinfo=UTC)  # 1 s before the 15:30 bar ends
    t = [sent_at]

    class SlowInfo:
        def info(self, payload: dict[str, Any]) -> Any:
            t[0] = sent_at + timedelta(seconds=7)  # response arrives after close + grace
            return snapshot_30m

    r = ingest_rest(
        sqlite_sessions,
        SlowInfo(),
        coin=COIN,
        interval=IV,
        kind="gap_fill",
        start=sent_at - timedelta(hours=2),
        end=sent_at,
        clock=lambda: t[0],
    )
    assert r.ok
    with sqlite_sessions() as s:
        last = s.scalar(select(CandleRow).order_by(CandleRow.open_time.desc()))
        assert last is not None and last.is_closed is False  # partial data is NOT frozen


def test_h1_bar_closes_when_request_is_sent_after_close(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    sent_at = datetime(2026, 11, 3, 16, 0, 5, tzinfo=UTC)  # exactly close + grace
    r = ingest_rest(
        sqlite_sessions,
        SnapshotInfo({"30m": snapshot_30m}),
        coin=COIN,
        interval=IV,
        kind="finalize",
        start=sent_at - timedelta(hours=2),
        end=sent_at,
        clock=lambda: sent_at,
    )
    assert r.ok and r.closed_now >= 1
    with sqlite_sessions() as s:
        last = s.scalar(select(CandleRow).order_by(CandleRow.open_time.desc()))
        assert last is not None and last.is_closed is True


# --------------------------------------------------------------------------- L2 / L3


def test_l2_older_rest_snapshot_does_not_rewind_forming_bar(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    newest = dict(
        snapshot_30m[-1], v=str(Decimal(snapshot_30m[-1]["v"]) + 100), n=snapshot_30m[-1]["n"] + 50
    )
    with sqlite_sessions.begin() as s:
        upsert_candles(s, _bars([newest]), source="ws", now=RECORDED_AT)
    with sqlite_sessions.begin() as s:  # older REST snapshot, bar still open
        st = upsert_candles(s, _bars(snapshot_30m[-1:]), source="rest", now=RECORDED_AT)
    assert st.ignored_out_of_order == 1
    with sqlite_sessions() as s:
        row = s.scalar(select(CandleRow))
        assert row is not None and row.volume == Decimal(newest["v"]) and not row.is_closed
    # ...but REST is the source of truth when it CLOSES the bar.
    later = RECORDED_AT + timedelta(hours=1)
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, _bars(snapshot_30m[-1:]), source="rest", now=later)
    assert st.closed_now == 1
    with sqlite_sessions() as s:
        row = s.scalar(select(CandleRow))
        assert row is not None and row.is_closed and row.volume == Decimal(snapshot_30m[-1]["v"])


def test_l3_db_failure_while_storing_marks_run_failed(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autotrader.ingest import rest

    def boom(*a: Any, **k: Any) -> Any:
        raise OperationalError("UPDATE", {}, Exception("server closed the connection"))

    monkeypatch.setattr(rest, "upsert_candles", boom)
    r = ingest_rest(
        sqlite_sessions,
        SnapshotInfo({"30m": snapshot_30m}),
        coin=COIN,
        interval=IV,
        kind="gap_fill",
        start=RECORDED_AT - timedelta(hours=1),
        end=RECORDED_AT,
        clock=Clock(),
    )
    assert not r.ok and (r.error or "").startswith("DATABASE")
    with sqlite_sessions() as s:
        assert [x.status for x in s.scalars(select(IngestionRun))] == ["FAILED"]
        assert s.scalar(select(func.count()).select_from(CandleRow)) == 0


# --------------------------------------------------------------------------- L4 / L5 / L8


def test_l4_untimed_rejections_with_different_reasons_are_kept_separately(
    sqlite_sessions: sessionmaker[Session],
) -> None:
    rej = [
        Rejected(CandleRejection.MALFORMED, "candle is not an object", "x"),
        Rejected(CandleRejection.WRONG_MARKET, "got CL", {"s": "CL"}),
        Rejected(CandleRejection.MISALIGNED_TIME, "t not on boundary", {"t": 1}),
    ]
    with sqlite_sessions.begin() as s:
        record_rejections(s, COIN, IV, rej, RECORDED_AT)
    with sqlite_sessions() as s:
        details = sorted(i.detail.split(":")[0] for i in list_issues(s, COIN, IV))
    assert details == ["MALFORMED", "MISALIGNED_TIME", "WRONG_MARKET"]


@pytest.mark.parametrize("n", [3.0, True, -1, 2**31, "303"])
def test_l5_trade_count_is_strict(n: Any, snapshot_30m: Any) -> None:
    r = parse_candle(dict(snapshot_30m[0], n=n), coin=COIN, interval=IV)
    assert isinstance(r, Rejected) and r.reason == CandleRejection.MALFORMED


def test_l8_duplicate_json_keys_rejected() -> None:
    with pytest.raises(SafetyViolationError, match="Duplicate"):
        check_ws_message(
            '{"method":"post","method":"subscribe","subscription":{"type":"trades","coin":"xyz:CL"}}'
        )
    with pytest.raises(SafetyViolationError, match="Duplicate"):
        check_request_allowed("POST", INFO_URL, b'{"type":"meta","type":"perpDexs"}')


# --------------------------------------------------------------------------- M1 / M2 / M4


def test_m1_backoff_resets_after_a_successful_session(
    sqlite_sessions: sessionmaker[Session],
    snapshot_30m: Any,
    snapshot_1h: Any,
    ws_frames: list[str],
) -> None:
    drops = [FakeConn([ws_frames[2], ConnectionResetError("drop")]) for _ in range(6)]
    connector = Connector([*drops, FakeConn([*ws_frames, HANG])])
    c = make(sqlite_sessions, SnapshotInfo({"30m": snapshot_30m, "1h": snapshot_1h}), connector)
    attempts: list[int] = []
    real = c.backoff_delay
    c.backoff_delay = lambda a: (attempts.append(a), real(a))[1]  # type: ignore[method-assign]
    run_until(c, lambda: connector.attempts >= 7 and c.state.ws_state == "CONNECTED")
    # Every drop happened after a successful connect -> backoff always restarts at 1.
    assert attempts[:6] == [1, 1, 1, 1, 1, 1]


def test_m2_slow_rest_sync_does_not_stall_ws_or_force_reconnects(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any, snapshot_1h: Any
) -> None:
    class SlowInfo(SnapshotInfo):
        def info(self, payload: dict[str, Any]) -> Any:
            time.sleep(0.8)  # each REST call is slower than stale_after (0.4 s)
            return super().info(payload)

    class Ticker(FakeConn):
        async def recv(self) -> str:
            await asyncio.sleep(0.02)  # healthy stream: a frame every 20 ms (< recv poll)
            return json.dumps({"channel": "pong"})

    c = make(
        sqlite_sessions, SlowInfo({"30m": snapshot_30m, "1h": snapshot_1h}), Connector([Ticker([])])
    )
    run_until(c, lambda: c.state.messages > 60, timeout=12)
    assert c.state.reconnects == 0, c.state.recovered_error or c.state.last_error


def test_m4_no_writes_after_run_returns(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any, snapshot_1h: Any
) -> None:
    gate = threading.Event()
    calls = {"n": 0}

    class GatedInfo(SnapshotInfo):
        def info(self, payload: dict[str, Any]) -> Any:
            calls["n"] += 1
            if calls["n"] > 2:  # let the startup sync pass, then block the next sync
                gate.wait(5)
            return super().info(payload)

    c = make(
        sqlite_sessions,
        GatedInfo({"30m": snapshot_30m, "1h": snapshot_1h}),
        Connector([FakeConn([HANG])]),
    )

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(c.run(stop))
        while calls["n"] <= 2:
            await asyncio.sleep(0.02)
        stop.set()
        # run() must not return while the blocked sync could still write.
        await asyncio.sleep(0.3)
        assert not task.done()
        gate.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    with sqlite_sessions() as s:
        before = s.scalar(select(func.count()).select_from(CandleRow))
        runs = s.scalars(select(IngestionRun)).all()
    assert all(r.status in ("OK", "FAILED") for r in runs)  # nothing left RUNNING
    time.sleep(0.5)
    with sqlite_sessions() as s:
        assert s.scalar(select(func.count()).select_from(CandleRow)) == before
    assert c.sync_rest(force=True) is False  # refuses to start after shutdown


def test_m4_background_safety_violation_stops_collector(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any, snapshot_1h: Any
) -> None:
    class Evil(SnapshotInfo):
        calls = 0

        def info(self, payload: dict[str, Any]) -> Any:
            Evil.calls += 1
            if Evil.calls > 2:  # the post-connect background gap fill
                raise SafetyViolationError("simulated")
            return super().info(payload)

    c = make(
        sqlite_sessions,
        Evil({"30m": snapshot_30m, "1h": snapshot_1h}),
        Connector([FakeConn([HANG])]),
    )
    with pytest.raises(SafetyViolationError):
        asyncio.run(asyncio.wait_for(c.run(asyncio.Event()), 10))


# --------------------------------------------------------------------------- M3 (API)


def test_m3_leftover_forming_bar_is_not_presented_as_live(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    with sqlite_sessions.begin() as s:
        upsert_candles(s, _bars(snapshot_30m), source="rest", now=RECORDED_AT)
    # Collector died: 10 h later the 15:30 bar is still the stored "forming" row.
    later = Clock(RECORDED_AT + timedelta(hours=10))
    body = (
        TestClient(create_app(Settings(), sqlite_sessions, clock=later))
        .get("/api/candles", params={"interval": "30m"})
        .json()
    )
    assert body["forming"] is None
    assert body["stale_forming_open_time"] == "2026-11-03T15:30:00+00:00"
    assert body["fresh"] is False
    # At recording time the same bar IS the current forming bar.
    body = (
        TestClient(create_app(Settings(), sqlite_sessions, clock=Clock()))
        .get("/api/candles", params={"interval": "30m"})
        .json()
    )
    assert body["forming"]["open_time"] == "2026-11-03T15:30:00+00:00"
    assert body["fresh"] is True and body["stale_forming_open_time"] is None


def test_m3_rejected_alert_only_for_recent_rejections(
    sqlite_sessions: sessionmaker[Session],
) -> None:
    with sqlite_sessions.begin() as s:
        record_rejections(s, COIN, IV, [Rejected(CandleRejection.MALFORMED, "x", "x")], RECORDED_AT)
    codes = lambda clock: {  # noqa: E731
        a["code"]
        for a in TestClient(create_app(Settings(), sqlite_sessions, clock=clock))
        .get("/api/collector/status")
        .json()["alerts"]
    }
    assert "REJECTED_CANDLES" in codes(Clock(RECORDED_AT + timedelta(hours=1)))
    assert "REJECTED_CANDLES" not in codes(Clock(RECORDED_AT + timedelta(days=2)))
