"""Synthetic-bar helpers for engine tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from autotrader.backtest.costs import CostModel
from autotrader.backtest.engine import (
    BacktestResult,
    Engine,
    ExecConfig,
    FundingBook,
    RiskConfig,
)
from autotrader.backtest.validation import audit
from autotrader.strategy.base import Bar, Decision, Side

D = Decimal
ONE = D(1)
STOP_ATR = D("1.5")
TP_R = D(2)
#: Tuesday 2026-10-13 14:00 UTC = 10:00 New York (reference session open, no holiday/roll)
T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
STEP = timedelta(minutes=30)


def bars(rows: Sequence[tuple[Any, ...]], start: datetime = T0) -> list[Bar]:
    """rows: (open, high, low, close[, volume]) - consecutive 30m bars."""
    out = []
    t = start
    for r in rows:
        o, h, lo, c = (D(str(x)) for x in r[:4])
        v = D(str(r[4])) if len(r) > 4 else D(1000)
        out.append(Bar(t, t + STEP, o, h, lo, c, v, 10))
        t += STEP
    return out


def flat(n: int, px: float = 80, start: datetime = T0) -> list[Bar]:
    return bars([(px, px + 0.1, px - 0.1, px)] * n, start)


class Scripted:
    """A Prepared that emits predetermined decisions (to test the engine alone)."""

    def __init__(self, signals: dict[int, Side], atr: Decimal = ONE) -> None:
        self.signals = signals
        self.atr = atr
        self.calls: list[int] = []

    def decide(self, i: int) -> Decision:
        self.calls.append(i)
        side = self.signals.get(i)
        if side is None:
            return Decision(None, "NO_SETUP")
        return Decision(side, "SIGNAL", self.atr, {"rule": "scripted"})


def run(
    b: Sequence[Bar],
    signals: dict[int, Side] | Scripted,
    *,
    costs: CostModel | None = None,
    risk: RiskConfig | None = None,
    ex: ExecConfig | None = None,
    funding: dict[datetime, Decimal] | None = None,
    stop_atr: Decimal = STOP_ATR,
    tp_r: Decimal = TP_R,
    max_hold: int | None = None,
    atr: Decimal = ONE,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[BacktestResult, list[str]]:
    prep = signals if isinstance(signals, Scripted) else Scripted(signals, atr)
    r = risk or RiskConfig()
    e = ex or ExecConfig()
    if not funding:
        # synthetic tests without funding data: don't fail closed on the missing feed
        e = replace(e, require_funding_data=False)
    res = Engine(
        b,
        prep,
        stop_atr=stop_atr,
        tp_r=tp_r,
        max_hold_bars=max_hold,
        costs=costs or CostModel(),
        risk=r,
        execution=e,
        funding=FundingBook(funding or {}),
        trade_start=start,
        trade_end=end,
    ).run()
    return res, audit(res, b, r, e.sz_decimals)


NO_COSTS = CostModel(
    taker_fee_rate=D(0), half_spread_rate=D(0), slippage_rate=D(0), missing_funding_rate=D(0)
)
