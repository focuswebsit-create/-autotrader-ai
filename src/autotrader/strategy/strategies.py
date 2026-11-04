"""The three initial research strategies for ``xyz:CL`` (deterministic, parametrized).

None of them is assumed to be profitable. They are hypotheses to be tested:

* ``ema_rsi``          EMA(20)/EMA(50) crossover on closed 30m bars, RSI(14) filter.
* ``trend_pullback``   Trend following: 1h EMA(20)/EMA(50) trend with a rising /
                       falling 1h EMA(50); entry when a closed 30m bar reclaims the
                       30m EMA(20) after a pullback, in the trend direction.
* ``breakout_volume``  The blueprint's "Trend-Filtered 30m Breakout": 1h EMA trend,
                       close beyond the prior-20-bar high/low, RSI band, volume >=
                       1.2 x prior-20-bar median volume.

Stops/targets (all strategies): stop = ``stop_atr`` x ATR(14) of the signal bar
from the ACTUAL simulated fill; target = ``tp_r`` x R. Both are applied by the
engine, never by the strategy.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar

from autotrader.strategy import indicators as ind
from autotrader.strategy.base import (
    DATA_GAP,
    NO_SETUP,
    TREND_DATA_MISSING,
    WARMUP,
    Bar,
    Decision,
    Side,
    StrategyParams,
    TrendIndex,
    contiguous_segments,
    fmt,
    run_length,
)

Arr = list[Decimal | None]


def _seg(bars: Sequence[Bar], fn: Callable[[Sequence[Bar]], Arr]) -> Arr:
    """Compute an indicator separately on each gap-free segment (reset after a gap)."""
    out: Arr = [None] * len(bars)
    for s, e in contiguous_segments(bars):
        out[s:e] = fn(bars[s:e])
    return out


def _closes(b: Sequence[Bar]) -> list[Decimal]:
    return [x.close for x in b]


def _ema(n: int) -> Callable[[Sequence[Bar]], Arr]:
    return lambda b: ind.ema(_closes(b), n)


def _rsi(n: int) -> Callable[[Sequence[Bar]], Arr]:
    return lambda b: ind.rsi(_closes(b), n)


def _atr(n: int) -> Callable[[Sequence[Bar]], Arr]:
    return lambda b: ind.atr([x.high for x in b], [x.low for x in b], _closes(b), n)


def _in(x: Decimal, lo: Decimal, hi: Decimal) -> bool:
    return lo <= x <= hi


# =========================================================================== EMA + RSI


@dataclass(frozen=True)
class EmaRsiParams(StrategyParams):
    fast: int = 20
    slow: int = 50
    rsi_len: int = 14
    long_rsi_min: Decimal = Decimal(50)
    long_rsi_max: Decimal = Decimal(70)
    short_rsi_min: Decimal = Decimal(30)
    short_rsi_max: Decimal = Decimal(50)

    def validate(self) -> None:
        super().validate()
        if not 2 <= self.fast < self.slow <= 400:
            raise ValueError("need 2 <= fast < slow <= 400")
        if self.rsi_len < 2:
            raise ValueError("rsi_len must be >= 2")
        if not (0 <= self.long_rsi_min < self.long_rsi_max <= 100):
            raise ValueError("bad long RSI band")
        if not (0 <= self.short_rsi_min < self.short_rsi_max <= 100):
            raise ValueError("bad short RSI band")


class _EmaRsiPrepared:
    def __init__(self, p: EmaRsiParams, m30: Sequence[Bar]) -> None:
        self.p = p
        self.bars = m30
        self.fast = _seg(m30, _ema(p.fast))
        self.slow = _seg(m30, _ema(p.slow))
        self.rsi = _seg(m30, _rsi(p.rsi_len))
        self.atr = _seg(m30, _atr(p.atr_len))
        self.run = run_length(m30)
        self.need = max(p.slow, p.rsi_len + 1, p.atr_len) + 1

    def decide(self, i: int) -> Decision:
        p = self.p
        if self.run[i] < self.need:
            return Decision(None, DATA_GAP if i >= self.need else WARMUP)
        f, s, f0, s0 = self.fast[i], self.slow[i], self.fast[i - 1], self.slow[i - 1]
        r, a = self.rsi[i], self.atr[i]
        if f is None or s is None or f0 is None or s0 is None or r is None or a is None:
            return Decision(None, WARMUP)
        why = {
            f"ema{p.fast}": fmt(f),
            f"ema{p.slow}": fmt(s),
            f"prev_ema{p.fast}": fmt(f0),
            f"prev_ema{p.slow}": fmt(s0),
            "rsi": fmt(r, 2),
            "atr": fmt(a),
            "close": str(self.bars[i].close),
        }
        if f > s and f0 <= s0 and _in(r, p.long_rsi_min, p.long_rsi_max):
            return Decision(Side.LONG, "SIGNAL", a, why | {"rule": "EMA cross up + RSI in band"})
        if f < s and f0 >= s0 and _in(r, p.short_rsi_min, p.short_rsi_max):
            return Decision(Side.SHORT, "SIGNAL", a, why | {"rule": "EMA cross down + RSI in band"})
        return Decision(None, NO_SETUP)


class EmaRsiStrategy:
    id: ClassVar[str] = "ema_rsi"
    version: ClassVar[str] = "1.0.0"
    title_he: ClassVar[str] = "EMA 20/50 + RSI"
    description_he: ClassVar[str] = (
        "חציית EMA 20 מעל/מתחת EMA 50 בנר 30 דקות סגור, עם RSI(14) בטווח "
        "50–70 ללונג או 30–50 לשורט. סטופ 1.5×ATR, יעד 2.5R."
    )

    def __init__(self, params: EmaRsiParams | None = None) -> None:
        self.params: EmaRsiParams = params or EmaRsiParams()
        self.params.validate()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.slow, p.rsi_len + 1, p.atr_len) + 1

    def prepare(self, m30: Sequence[Bar], h1: Sequence[Bar]) -> _EmaRsiPrepared:
        return _EmaRsiPrepared(self.params, m30)


# =========================================================================== Trend following


@dataclass(frozen=True)
class TrendPullbackParams(StrategyParams):
    trend_fast: int = 20
    trend_slow: int = 50
    #: 1h EMA(slow) must have risen (fallen) over this many 1h bars
    slope_bars: int = 3
    entry_ema: int = 20
    filter_ema: int = 50
    stop_atr: Decimal = Decimal("2.0")
    tp_r: Decimal = Decimal("2.0")

    def validate(self) -> None:
        super().validate()
        if not 2 <= self.trend_fast < self.trend_slow <= 400:
            raise ValueError("need 2 <= trend_fast < trend_slow <= 400")
        if not 1 <= self.slope_bars <= 48:
            raise ValueError("slope_bars out of range")
        if not 2 <= self.entry_ema < self.filter_ema <= 400:
            raise ValueError("need 2 <= entry_ema < filter_ema <= 400")


class _TrendPullbackPrepared:
    def __init__(self, p: TrendPullbackParams, m30: Sequence[Bar], h1: Sequence[Bar]) -> None:
        self.p = p
        self.bars = m30
        self.e_entry = _seg(m30, _ema(p.entry_ema))
        self.e_filter = _seg(m30, _ema(p.filter_ema))
        self.atr = _seg(m30, _atr(p.atr_len))
        self.run = run_length(m30)
        self.need = max(p.filter_ema, p.atr_len) + 1
        self.h_fast = _seg(h1, _ema(p.trend_fast))
        self.h_slow = _seg(h1, _ema(p.trend_slow))
        self.h_run = run_length(h1)
        self.h1_open = [x.open_time.isoformat() for x in h1]
        self.trend = TrendIndex(h1)

    def decide(self, i: int) -> Decision:
        p, b = self.p, self.bars
        if self.run[i] < self.need:
            return Decision(None, DATA_GAP if i >= self.need else WARMUP)
        e, e0, flt, a = self.e_entry[i], self.e_entry[i - 1], self.e_filter[i], self.atr[i]
        if e is None or e0 is None or flt is None or a is None:
            return Decision(None, WARMUP)
        j = self.trend.index_at(b[i].close_time)
        if j is None or self.h_run[j] < p.trend_slow + p.slope_bars:
            return Decision(None, TREND_DATA_MISSING)
        hf, hs, hs_prev = self.h_fast[j], self.h_slow[j], self.h_slow[j - p.slope_bars]
        if hf is None or hs is None or hs_prev is None:
            return Decision(None, TREND_DATA_MISSING)
        c, c0 = b[i].close, b[i - 1].close
        why = {
            f"h1_ema{p.trend_fast}": fmt(hf),
            f"h1_ema{p.trend_slow}": fmt(hs),
            f"h1_ema{p.trend_slow}_{p.slope_bars}_bars_ago": fmt(hs_prev),
            "h1_bar_open": self.h1_open[j],
            f"ema{p.entry_ema}": fmt(e),
            f"ema{p.filter_ema}": fmt(flt),
            "close": str(c),
            "prev_close": str(c0),
            "atr": fmt(a),
        }
        if hf > hs and hs > hs_prev and c0 <= e0 and c > e and c > flt:
            return Decision(Side.LONG, "SIGNAL", a, why | {"rule": "1h uptrend + reclaim EMA"})
        if hf < hs and hs < hs_prev and c0 >= e0 and c < e and c < flt:
            return Decision(Side.SHORT, "SIGNAL", a, why | {"rule": "1h downtrend + lose EMA"})
        return Decision(None, NO_SETUP)


class TrendPullbackStrategy:
    id: ClassVar[str] = "trend_pullback"
    version: ClassVar[str] = "1.0.0"
    title_he: ClassVar[str] = "Trend Following (מגמה + חזרה ל־EMA)"
    description_he: ClassVar[str] = (
        "מגמה לפי נרות שעה סגורים: EMA 20 מעל/מתחת EMA 50 ו־EMA 50 עולה/יורד. "
        "כניסה כשנר 30 דקות סגור חוזר מעל/מתחת EMA 20 אחרי תיקון. סטופ 2×ATR, יעד 2R."
    )

    def __init__(self, params: TrendPullbackParams | None = None) -> None:
        self.params: TrendPullbackParams = params or TrendPullbackParams()
        self.params.validate()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.filter_ema, p.atr_len) + 1

    def prepare(self, m30: Sequence[Bar], h1: Sequence[Bar]) -> _TrendPullbackPrepared:
        return _TrendPullbackPrepared(self.params, m30, h1)


# =========================================================================== Breakout + volume


@dataclass(frozen=True)
class BreakoutParams(StrategyParams):
    trend_fast: int = 20
    trend_slow: int = 50
    lookback: int = 20
    rsi_len: int = 14
    long_rsi_min: Decimal = Decimal(52)
    long_rsi_max: Decimal = Decimal(70)
    short_rsi_min: Decimal = Decimal(30)
    short_rsi_max: Decimal = Decimal(48)
    volume_mult: Decimal = Decimal("1.2")
    volume_lookback: int = 20

    def validate(self) -> None:
        super().validate()
        if not 2 <= self.trend_fast < self.trend_slow <= 400:
            raise ValueError("need 2 <= trend_fast < trend_slow <= 400")
        if not 2 <= self.lookback <= 500 or not 2 <= self.volume_lookback <= 500:
            raise ValueError("lookback out of range")
        if not Decimal(0) <= self.volume_mult <= Decimal(20):
            raise ValueError("volume_mult out of range")
        if not (0 <= self.long_rsi_min < self.long_rsi_max <= 100):
            raise ValueError("bad long RSI band")
        if not (0 <= self.short_rsi_min < self.short_rsi_max <= 100):
            raise ValueError("bad short RSI band")


class _BreakoutPrepared:
    def __init__(self, p: BreakoutParams, m30: Sequence[Bar], h1: Sequence[Bar]) -> None:
        self.p = p
        self.bars = m30
        self.rsi = _seg(m30, _rsi(p.rsi_len))
        self.atr = _seg(m30, _atr(p.atr_len))
        self.hi = _seg(m30, lambda b: ind.prior_max([x.high for x in b], p.lookback))
        self.lo = _seg(m30, lambda b: ind.prior_min([x.low for x in b], p.lookback))
        self.vmed = _seg(m30, lambda b: ind.prior_median([x.volume for x in b], p.volume_lookback))
        self.run = run_length(m30)
        self.need = max(p.lookback, p.volume_lookback, p.rsi_len, p.atr_len) + 1
        self.h_fast = _seg(h1, _ema(p.trend_fast))
        self.h_slow = _seg(h1, _ema(p.trend_slow))
        self.h_run = run_length(h1)
        self.h1_open = [x.open_time.isoformat() for x in h1]
        self.trend = TrendIndex(h1)

    def decide(self, i: int) -> Decision:
        p, bar = self.p, self.bars[i]
        if self.run[i] < self.need:
            return Decision(None, DATA_GAP if i >= self.need else WARMUP)
        r, a, hi, lo, vm = self.rsi[i], self.atr[i], self.hi[i], self.lo[i], self.vmed[i]
        if r is None or a is None or hi is None or lo is None or vm is None:
            return Decision(None, WARMUP)
        j = self.trend.index_at(bar.close_time)
        if j is None or self.h_run[j] < p.trend_slow:
            return Decision(None, TREND_DATA_MISSING)
        hf, hs = self.h_fast[j], self.h_slow[j]
        if hf is None or hs is None:
            return Decision(None, TREND_DATA_MISSING)
        vol_ok = bar.volume >= p.volume_mult * vm
        why = {
            f"h1_ema{p.trend_fast}": fmt(hf),
            f"h1_ema{p.trend_slow}": fmt(hs),
            "h1_bar_open": self.h1_open[j],
            f"prior_{p.lookback}_high": str(hi),
            f"prior_{p.lookback}_low": str(lo),
            "close": str(bar.close),
            "rsi": fmt(r, 2),
            "volume": str(bar.volume),
            f"median_volume_{p.volume_lookback}": fmt(vm, 3),
            "atr": fmt(a),
        }
        if hf > hs and bar.close > hi and _in(r, p.long_rsi_min, p.long_rsi_max) and vol_ok:
            return Decision(Side.LONG, "SIGNAL", a, why | {"rule": "uptrend + breakout + volume"})
        if hf < hs and bar.close < lo and _in(r, p.short_rsi_min, p.short_rsi_max) and vol_ok:
            return Decision(
                Side.SHORT, "SIGNAL", a, why | {"rule": "downtrend + breakdown + volume"}
            )
        return Decision(None, NO_SETUP)


class BreakoutVolumeStrategy:
    id: ClassVar[str] = "breakout_volume"
    version: ClassVar[str] = "1.0.0"
    title_he: ClassVar[str] = "Breakout + Volume (פריצה עם נפח)"
    description_he: ClassVar[str] = (
        "השערת הבסיס מהמפרט: מגמת שעה (EMA 20/50), סגירת נר 30 דקות מעל השיא/מתחת "
        "לשפל של 20 הנרות הקודמים, RSI(14) בטווח 52–70 / 30–48, ונפח ≥ 1.2× חציון "
        "20 הנרות הקודמים. סטופ 1.5×ATR, יעד 2.5R."
    )

    def __init__(self, params: BreakoutParams | None = None) -> None:
        self.params: BreakoutParams = params or BreakoutParams()
        self.params.validate()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.lookback, p.volume_lookback, p.rsi_len, p.atr_len) + 1

    def prepare(self, m30: Sequence[Bar], h1: Sequence[Bar]) -> _BreakoutPrepared:
        return _BreakoutPrepared(self.params, m30, h1)


# =========================================================================== registry

STRATEGIES: dict[str, tuple[type[Any], type[StrategyParams]]] = {
    EmaRsiStrategy.id: (EmaRsiStrategy, EmaRsiParams),
    TrendPullbackStrategy.id: (TrendPullbackStrategy, TrendPullbackParams),
    BreakoutVolumeStrategy.id: (BreakoutVolumeStrategy, BreakoutParams),
}


def build_strategy(strategy_id: str, params: StrategyParams | None = None) -> Any:
    try:
        cls, pcls = STRATEGIES[strategy_id]
    except KeyError as exc:
        raise ValueError(f"unknown strategy {strategy_id!r}") from exc
    if params is not None and not isinstance(params, pcls):
        raise TypeError(f"{strategy_id} expects {pcls.__name__}")
    return cls(params)


def default_params(strategy_id: str) -> StrategyParams:
    return STRATEGIES[strategy_id][1]()
