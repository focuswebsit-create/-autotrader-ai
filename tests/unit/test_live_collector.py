"""Live collector behaviour under real-world failures (fake WS + recorded data).

Covers: subscribe + ingest, disconnect -> reconnect -> REST gap fill, stale
stream detection, connect failures with backoff, DB outage, restart
persistence, rejected / out-of-order frames, and safety violations.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.safety import SafetyViolationError
from autotrader.common.ws import GuardedConnection
from autotrader.db.market_data import get_heartbeat, list_issues
from autotrader.db.models import CandleRow, IngestionRun
from autotrader.ingest.live import SERVICE_NAME, CollectorConfig, LiveCollector
from autotrader.market.candles import Interval
from tests.conftest import RECORDED_AT

COIN = "xyz:CL"
HANG = object()


class ShiftedClock:
    """RECORDED_AT + real elapsed time: deterministic dates, real timeouts."""

    def __init__(self, start: datetime = RECORDED_AT) -> None:
        self.start, self.t0 = start, time.monotonic()

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=time.monotonic() - self.t0)


class FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.script or self.script[0] is HANG:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True


class Connector:
    """Returns scripted connections (wrapped in the real guard); exceptions = connect fails."""

    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.conns: list[FakeConn] = []
        self.attempts = 0

    async def __call__(self) -> GuardedConnection:
        self.attempts += 1
        item = self.items.pop(0) if self.items else FakeConn([HANG])
        if isinstance(item, BaseException):
            raise item
        self.conns.append(item)
        return GuardedConnection(item)


class SnapshotInfo:
    def __init__(self, by_interval: dict[str, Any], fail: bool = False) -> None:
        self.by_interval, self.fail = by_interval, fail
        self.calls: list[dict[str, Any]] = []

    def info(self, payload: dict[str, Any]) -> Any:
        from autotrader.common.http import InfoApiError

        self.calls.append(payload)
        if self.fail:
            raise InfoApiError("down")
        return self.by_interval[payload["req"]["interval"]]


def fast_config(**over: Any) -> CollectorConfig:
    cfg = CollectorConfig(
        coin=COIN,
        stale_after=timedelta(seconds=0.4),
        ping_every=timedelta(seconds=0.15),
        sync_every=timedelta(seconds=0.2),
        heartbeat_every=timedelta(seconds=0.05),
        backoff_initial_s=0.02,
        backoff_max_s=0.1,
        recv_poll_s=0.05,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def run_until(collector: LiveCollector, cond: Callable[[], bool], timeout: float = 8.0) -> None:
    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(collector.run(stop))
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline and not task.done():
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())


@pytest.fixture
def info(snapshot_30m: Any, snapshot_1h: Any) -> SnapshotInfo:
    return SnapshotInfo({"30m": snapshot_30m, "1h": snapshot_1h})


def make(
    sessions: sessionmaker[Session], info: Any, connector: Connector, **cfg: Any
) -> LiveCollector:
    return LiveCollector(
        fast_config(**cfg),
        sessions=sessions,
        info=info,
        connect=connector,
        clock=ShiftedClock(),
        rng=random.Random(7),
    )


def candle_count(sessions: sessionmaker[Session]) -> int:
    with sessions() as s:
        return int(s.scalar(select(func.count()).select_from(CandleRow)) or 0)


def test_subscribes_ingests_and_reports_state(
    sqlite_sessions: sessionmaker[Session],
    info: SnapshotInfo,
    ws_frames: list[str],
    snapshot_30m: Any,
) -> None:
    # The recorded WS frames are a few minutes OLDER than the recorded REST
    # snapshot (lower volume), so they must be ignored; a newer frame applies.
    newer = dict(snapshot_30m[-1])
    newer["v"] = str(float(newer["v"]) + 10)
    newer["n"] = newer["n"] + 1
    frames = [*ws_frames, {"channel": "candle", "data": newer}, HANG]
    connector = Connector([FakeConn(frames)])
    c = make(sqlite_sessions, info, connector)
    run_until(c, lambda: c.state.messages >= len(ws_frames) + 1)
    assert c.state.ignored_out_of_order == 2

    sent = [json.loads(m) for m in connector.conns[0].sent]
    subs = [m["subscription"] for m in sent if m["method"] == "subscribe"]
    assert {"type": "candle", "coin": COIN, "interval": "30m"} in subs
    assert {"type": "candle", "coin": COIN, "interval": "1h"} in subs
    assert {"type": "activeAssetCtx", "coin": COIN} in subs
    assert c.state.market_ctx["markPx"] == "96.201"
    assert c.state.last_candle_update_at is not None
    # 201 bars per interval from REST; WS frames updated existing forming bars.
    assert candle_count(sqlite_sessions) == 402
    with sqlite_sessions() as s:
        hb = get_heartbeat(s, SERVICE_NAME)
        assert hb is not None and hb.state == "STOPPED" and hb.ws_state == "DISCONNECTED"
        runs = s.scalars(select(IngestionRun)).all()
        assert {r.kind for r in runs} >= {"backfill"} and all(r.status == "OK" for r in runs)


def test_disconnect_triggers_reconnect_and_rest_gap_fill(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, ws_frames: list[str]
) -> None:
    first = FakeConn([ws_frames[1], ConnectionResetError("network dropped")])
    second = FakeConn([*ws_frames, HANG])
    connector = Connector([first, second])
    c = make(sqlite_sessions, info, connector)
    run_until(c, lambda: connector.attempts >= 2 and c.state.ws_state == "CONNECTED")

    assert c.state.reconnects >= 1
    # After a successful reconnect the error is history, not a current problem.
    assert c.state.last_error is None
    assert "ConnectionResetError" in (c.state.recovered_error or "")
    assert c.state.recovered_at is not None
    with sqlite_sessions() as s:
        kinds = [r.kind for r in s.scalars(select(IngestionRun).order_by(IngestionRun.id))]
    # backfill at start, then a gap_fill after EACH (re)connect.
    assert kinds.count("gap_fill") >= 4  # 2 intervals x 2 connections
    assert candle_count(sqlite_sessions) == 402  # no duplicates after replays


def test_stale_stream_is_detected_and_recycled(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo
) -> None:
    silent = FakeConn([HANG])  # connects, then never sends anything
    connector = Connector([silent])
    c = make(sqlite_sessions, info, connector)
    seen_stale: list[bool] = []

    def cond() -> bool:
        seen_stale.append(c.state.ws_state == "STALE")
        return connector.attempts >= 2

    run_until(c, cond)
    assert silent.closed
    assert "no WS data" in (c.state.recovered_error or c.state.last_error or "")
    assert c.state.reconnects >= 1


def test_connect_failures_back_off_then_recover(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, ws_frames: list[str]
) -> None:
    connector = Connector([OSError("dns"), OSError("refused"), FakeConn([*ws_frames, HANG])])
    c = make(sqlite_sessions, info, connector)
    run_until(c, lambda: c.state.ws_state == "CONNECTED")
    assert connector.attempts == 3 and c.state.reconnects == 2
    assert c.state.last_error is None and "refused" in (c.state.recovered_error or "")


def test_backoff_is_exponential_jittered_and_capped() -> None:
    c = LiveCollector(
        CollectorConfig(coin=COIN, backoff_initial_s=1, backoff_max_s=60),
        sessions=None,  # type: ignore[arg-type]
        info=None,  # type: ignore[arg-type]
        connect=None,  # type: ignore[arg-type]
        rng=random.Random(1),
    )
    for attempt, cap in [(1, 1), (2, 2), (3, 4), (6, 32), (7, 60), (50, 60)]:
        d = c.backoff_delay(attempt)
        assert cap / 2 <= d <= cap


def test_out_of_order_frames_do_not_rewind_data(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, snapshot_30m: Any
) -> None:
    newest = dict(snapshot_30m[-1], v="999999", n=99999)
    older = dict(snapshot_30m[-1], v="1", n=1)
    older["h"], older["l"], older["c"] = older["o"], older["o"], older["o"]
    frames = [{"channel": "candle", "data": newest}, {"channel": "candle", "data": older}, HANG]
    c = make(sqlite_sessions, info, Connector([FakeConn(frames)]))
    run_until(c, lambda: c.state.messages >= 2)
    assert c.state.ignored_out_of_order >= 1
    with sqlite_sessions() as s:
        row = s.scalar(
            select(CandleRow).where(CandleRow.is_closed.is_(False), CandleRow.interval == "30m")
        )
        assert row is not None and str(row.volume) == "999999"


def test_invalid_ws_candle_is_recorded_not_stored(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, snapshot_30m: Any
) -> None:
    bad = dict(snapshot_30m[-1], l="-1")
    frames = [
        {"channel": "candle", "data": bad},
        "not json",
        {"channel": "candle", "data": {"i": "5m"}},
        HANG,
    ]
    c = make(sqlite_sessions, info, Connector([FakeConn(frames)]))
    run_until(c, lambda: c.state.messages >= 3)
    assert c.state.rejected == 2
    with sqlite_sessions() as s:
        assert [i.issue_type for i in list_issues(s, COIN, Interval.M30)] == ["REJECTED_CANDLE"]


def test_rest_outage_keeps_running_and_reports_degraded(
    sqlite_sessions: sessionmaker[Session], ws_frames: list[str]
) -> None:
    info = SnapshotInfo({}, fail=True)
    c = make(sqlite_sessions, info, Connector([FakeConn([*ws_frames, HANG])]))
    states: list[str] = []
    run_until(c, lambda: states.append(c.health_state()) or c.state.messages >= 3)
    assert c.state.last_sync_ok is False
    assert "DEGRADED" in states
    with sqlite_sessions() as s:
        assert {r.status for r in s.scalars(select(IngestionRun))} == {"FAILED"}


def test_database_outage_does_not_crash_collector(info: SnapshotInfo, ws_frames: list[str]) -> None:
    dead = sessionmaker(
        bind=create_engine("postgresql+psycopg://u:p@127.0.0.1:1/x?connect_timeout=1")
    )
    connector = Connector([FakeConn([*ws_frames, HANG]), FakeConn([*ws_frames, HANG])])
    c = make(dead, info, connector)
    run_until(c, lambda: connector.attempts >= 2, timeout=10)
    assert c.state.last_sync_ok is False
    assert c.state.reconnects >= 1  # DB error in a frame handler -> session recycled, not a crash


def test_restart_continues_without_duplicates(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, ws_frames: list[str]
) -> None:
    a = make(sqlite_sessions, info, Connector([FakeConn([*ws_frames, HANG])]))
    run_until(a, lambda: a.state.messages >= len(ws_frames))
    n1 = candle_count(sqlite_sessions)
    b = make(sqlite_sessions, info, Connector([FakeConn([*ws_frames, HANG])]))
    run_until(b, lambda: b.state.messages >= len(ws_frames))
    assert candle_count(sqlite_sessions) == n1 == 402
    with sqlite_sessions() as s:
        hb = get_heartbeat(s, SERVICE_NAME)
        assert hb is not None and hb.state == "STOPPED"


def test_safety_violation_stops_collector(
    sqlite_sessions: sessionmaker[Session], info: SnapshotInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make(sqlite_sessions, info, Connector([FakeConn([HANG])]))
    monkeypatch.setattr(c, "subscriptions", lambda: [{"type": "userFills", "user": "0x0"}])

    async def go() -> None:
        await asyncio.wait_for(c.run(asyncio.Event()), 10)

    with pytest.raises(SafetyViolationError):
        asyncio.run(go())
