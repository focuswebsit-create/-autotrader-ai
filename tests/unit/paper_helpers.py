"""Builders for PAPER engine tests: synthetic candles / books, scripted strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from autotrader.db.market_data import write_heartbeat
from autotrader.db.models import CandleRow, FundingRate, PaperAccount, PaperCommand
from autotrader.paper import engine
from autotrader.paper.book import L2Book
from autotrader.strategy.base import Decision, Side

D = Decimal
COIN = "xyz:CL"
STEP = timedelta(minutes=30)


def book(
    t: datetime, bid: str | Decimal, ask: str | Decimal, size: str = "50", levels: int = 20,
    tick: str = "0.01",
) -> L2Book:  # fmt: skip
    b, a, tk = D(str(bid)), D(str(ask)), D(tick)
    bids = tuple((b - i * tk, D(size)) for i in range(levels))
    asks = tuple((a + i * tk, D(size)) for i in range(levels))
    return L2Book(COIN, t, bids, asks)


def add_bars(s: Session, start: datetime, n: int, px: str = "80", interval: str = "30m") -> None:
    step = STEP if interval == "30m" else timedelta(hours=1)
    p = D(px)
    for i in range(n):
        o = start + i * step
        s.add(CandleRow(coin=COIN, interval=interval, open_time=o, close_time=o + step, open=p,
                        high=p + D("0.1"), low=p - D("0.1"), close=p, volume=D(1000), trades=10,
                        is_closed=True, source="rest", first_seen_at=o, updated_at=o,
                        closed_at=o + step))  # fmt: skip
    s.flush()


def collector_ok(s: Session, now: datetime) -> None:
    write_heartbeat(s, engine.COLLECTOR_SERVICE, state="RUNNING", ws_state="CONNECTED",
                    started_at=now - timedelta(hours=1), last_heartbeat_at=now,
                    details={"market_ctx": {"markPx": "80.0", "oraclePx": "80.0"}})  # fmt: skip


def funding(s: Session, hour: datetime, rate: str = "0.00001") -> None:
    s.add(FundingRate(coin=COIN, time=hour + timedelta(milliseconds=50), hour=hour,
                      rate=D(rate), premium=None, source="test", received_at=hour))  # fmt: skip
    s.flush()


@dataclass
class Params:
    stop_atr: Decimal = Decimal("1.5")
    tp_r: Decimal = Decimal(2)
    max_hold_bars: int | None = None


class ScriptedPrepared:
    def __init__(self, bars: list[Any], script: dict[datetime, Side]) -> None:
        self.bars, self.script = bars, script

    def decide(self, i: int) -> Decision:
        side = self.script.get(self.bars[i].open_time)
        if side is None:
            return Decision(None, "NO_SETUP")
        return Decision(side, "SIGNAL", D(1), {"rule": f"scripted {side.value}"})


class ScriptedStrategy:
    id = "breakout_volume"
    version = "1.0.0"
    title_he = "scripted"
    description_he = "scripted"

    def __init__(self, script: dict[datetime, Side]) -> None:
        self.script = script
        self.params = Params()

    def warmup_bars(self) -> int:
        return 1

    def prepare(self, m30: list[Any], h1: list[Any]) -> ScriptedPrepared:
        return ScriptedPrepared(m30, self.script)


def command(
    sessions: sessionmaker[Session], acc_id: int, cmd: str, now: datetime, key: str
) -> None:
    with sessions.begin() as s:
        s.add(PaperCommand(account_id=acc_id, command=cmd, requested_by="test", reason=cmd,
                           idempotency_key=key, requested_at=now))  # fmt: skip


def tick(sessions: sessionmaker[Session], acc_id: int, now: datetime, bk: L2Book | None) -> None:
    with sessions.begin() as s:
        acc = s.get(PaperAccount, acc_id)
        assert acc is not None
        engine.tick(s, acc, engine.Market(now=now, book=bk, ctx={"markPx": "80.0"}))


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, sec: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, sec, tzinfo=UTC)
