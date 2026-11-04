"""Shared strategy types. Used by BOTH the backtester and (Phase 3) the paper engine.

A strategy is a pure, deterministic function of CLOSED bars:

* :meth:`Strategy.prepare` computes causal features once for a series of closed
  30m bars and closed 1h bars;
* :meth:`Prepared.decide` returns the decision for 30m bar ``i`` using only
  bars ``<= i`` and 1h bars whose ``close_time`` is ``<=`` bar ``i``'s close.

Decisions never contain an execution price. The engine/broker decides how and
when a signal can be filled (next bar open, with costs), so a strategy cannot
"enter at the signal close".
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from autotrader.market.candles import Candle


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True, slots=True)
class Bar:
    """A CLOSED OHLCV bar (exclusive ``close_time``)."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int | None = None

    @classmethod
    def from_candle(cls, c: Candle) -> Bar:
        return cls(c.open_time, c.close_time, c.open, c.high, c.low, c.close, c.volume, c.trades)

    def key(self) -> str:
        return self.open_time.isoformat()


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of evaluating one closed bar."""

    side: Side | None
    #: "SIGNAL" or the reason no signal was produced (e.g. WARMUP, NO_SETUP)
    reason: str
    #: ATR of the signal bar (needed by the engine to place the stop)
    atr: Decimal | None = None
    #: human/audit readable indicator values that drove the decision
    rationale: dict[str, str] = field(default_factory=dict)


NO_SETUP = "NO_SETUP"
WARMUP = "WARMUP"
DATA_GAP = "DATA_GAP"
TREND_DATA_MISSING = "TREND_DATA_MISSING"


@dataclass(frozen=True)
class StrategyParams:
    """Base for parameter sets. Subclasses are frozen dataclasses of numbers."""

    atr_len: int = 14
    stop_atr: Decimal = Decimal("1.5")
    tp_r: Decimal = Decimal("2.5")
    #: optional time exit (in 30m bars); None = pure bracket
    max_hold_bars: int | None = None

    def validate(self) -> None:
        if self.atr_len < 2:
            raise ValueError("atr_len must be >= 2")
        if not Decimal("0.1") <= self.stop_atr <= Decimal(10):
            raise ValueError("stop_atr out of range")
        if not Decimal("0.2") <= self.tp_r <= Decimal(20):
            raise ValueError("tp_r out of range")
        if self.max_hold_bars is not None and self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be >= 1")

    def as_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    def replace(self, **changes: Any) -> StrategyParams:
        from dataclasses import replace

        p = replace(self, **changes)
        p.validate()
        return p

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]


def params_hash(strategy_id: str, version: str, params: StrategyParams) -> str:
    payload = json.dumps(
        {"strategy": strategy_id, "version": version, "params": params.as_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Prepared(Protocol):
    def decide(self, i: int) -> Decision: ...


class Strategy(Protocol):
    id: ClassVar[str]
    version: ClassVar[str]
    title_he: ClassVar[str]
    description_he: ClassVar[str]
    params: StrategyParams

    def prepare(self, m30: Sequence[Bar], h1: Sequence[Bar]) -> Prepared: ...

    def warmup_bars(self) -> int: ...


# --------------------------------------------------------------------------- helpers


def contiguous_segments(bars: Sequence[Bar]) -> list[tuple[int, int]]:
    """[start, end) index ranges in which consecutive bars have no time gap."""
    if not bars:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(bars)):
        if bars[i].open_time != bars[i - 1].close_time:
            out.append((start, i))
            start = i
    out.append((start, len(bars)))
    return out


def run_length(bars: Sequence[Bar]) -> list[int]:
    """For each i, how many consecutive gap-free bars end at i (including i)."""
    out = [0] * len(bars)
    for i in range(len(bars)):
        out[i] = 1 if i == 0 or bars[i].open_time != bars[i - 1].close_time else out[i - 1] + 1
    return out


class TrendIndex:
    """Map a 30m decision time to the latest CLOSED 1h bar (``close_time <= t``)."""

    def __init__(self, h1: Sequence[Bar], max_age: timedelta = timedelta(hours=1)) -> None:
        self._close_times = [b.close_time for b in h1]
        self._max_age = max_age

    def index_at(self, t: datetime) -> int | None:
        j = bisect.bisect_right(self._close_times, t) - 1
        if j < 0:
            return None
        # stale trend data (e.g. a 1h gap) is not used
        if t - self._close_times[j] >= self._max_age:
            return None
        return j


def fmt(value: Decimal | None, places: int = 4) -> str:
    if value is None:
        return "n/a"
    return str(value.quantize(Decimal(1).scaleb(-places)))
