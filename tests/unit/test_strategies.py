"""Strategy rules on synthetic bars, and no-look-ahead on real recorded data."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from autotrader.strategy.base import Bar, Side, TrendIndex, params_hash, run_length
from autotrader.strategy.strategies import (
    STRATEGIES,
    BreakoutParams,
    BreakoutVolumeStrategy,
    EmaRsiParams,
    EmaRsiStrategy,
    build_strategy,
    default_params,
)
from tests.unit.bt_helpers import STEP, T0, D, bars


def _h1_from(m30: list[Bar]) -> list[Bar]:
    """Aggregate contiguous 30m bars into aligned 1h bars (test helper)."""
    out = []
    for i in range(0, len(m30) - 1, 2):
        a, b = m30[i], m30[i + 1]
        if a.open_time.minute != 0:
            continue
        out.append(
            Bar(
                a.open_time,
                b.close_time,
                a.open,
                max(a.high, b.high),
                min(a.low, b.low),
                b.close,
                a.volume + b.volume,
            )
        )
    return out


def _trend_then_breakout(n: int = 260, up: bool = True, vol_spike: int = 5000) -> list[Bar]:
    """Steady trend with small pullbacks, then a breakout bar with a volume spike."""
    rows = []
    px = 80.0
    rng = random.Random(1)
    step = 0.02 if up else -0.02
    for i in range(n):
        drift = step + (rng.random() - 0.5) * 0.06
        o = px
        c = px + drift
        rows.append((round(o, 3), round(max(o, c) + 0.03, 3), round(min(o, c) - 0.03, 3),
                     round(c, 3), 1000 + (i % 7) * 10))  # fmt: skip
        px = c
    last_hi = max(r[1] for r in rows[-20:])
    last_lo = min(r[2] for r in rows[-20:])
    if up:
        c = last_hi + 0.15
        rows.append((round(px, 3), round(c + 0.02, 3), round(px - 0.01, 3), round(c, 3), vol_spike))
    else:
        c = last_lo - 0.15
        rows.append((round(px, 3), round(px + 0.01, 3), round(c - 0.02, 3), round(c, 3), vol_spike))
    rows.append((round(c, 3), round(c + 0.05, 3), round(c - 0.05, 3), round(c, 3), 1000))
    return bars(rows, start=T0 - 300 * STEP)


@pytest.mark.parametrize("up", [True, False])
def test_breakout_signals_with_trend_breakout_and_volume(up: bool) -> None:
    m30 = _trend_then_breakout(up=up)
    h1 = _h1_from(m30)
    p = BreakoutParams(long_rsi_max=D(100), short_rsi_min=D(0))
    d = BreakoutVolumeStrategy(p).prepare(m30, h1).decide(len(m30) - 2)
    assert d.side is (Side.LONG if up else Side.SHORT), d
    assert d.atr is not None and d.atr > 0
    assert "prior_20_high" in d.rationale and d.rationale["rule"]


def test_breakout_volume_filter_and_rsi_band_block() -> None:
    m30 = _trend_then_breakout(vol_spike=1000)  # no volume expansion
    h1 = _h1_from(m30)
    p = BreakoutParams(long_rsi_max=D(100))
    assert BreakoutVolumeStrategy(p).prepare(m30, h1).decide(len(m30) - 2).side is None
    # RSI band too narrow to include the breakout bar's RSI
    m30 = _trend_then_breakout()
    p2 = BreakoutParams(long_rsi_min=D(52), long_rsi_max=D(53))
    assert BreakoutVolumeStrategy(p2).prepare(m30, _h1_from(m30)).decide(len(m30) - 2).side is None


def test_breakout_needs_closed_trend_bar() -> None:
    m30 = _trend_then_breakout()
    # without any 1h data the trend filter can't be evaluated -> no signal
    d = BreakoutVolumeStrategy().prepare(m30, []).decide(len(m30) - 2)
    assert d.side is None and d.reason == "TREND_DATA_MISSING"


def test_trend_index_uses_only_closed_hour_bars() -> None:
    m30 = bars([(80, 80.1, 79.9, 80)] * 6)
    h1 = _h1_from(m30)
    idx = TrendIndex(h1)
    # at 14:30 the 14:00-15:00 bar is still forming -> latest closed is 13:00-14:00 (absent)
    assert idx.index_at(T0 + STEP) is None
    assert idx.index_at(T0 + 2 * STEP) == 0  # 15:00: the 14:00 bar just closed
    assert idx.index_at(T0 + 3 * STEP) == 0
    assert idx.index_at(T0 + 4 * STEP) == 1
    # stale: no newer 1h bar for > 1h -> not used
    assert idx.index_at(T0 + 10 * STEP) is None


def test_ema_rsi_cross_signal() -> None:
    rows = [(80 - i * 0.01, 80 - i * 0.01 + 0.02, 80 - i * 0.01 - 0.02, 80 - i * 0.01)
            for i in range(80)]  # fmt: skip
    px = rows[-1][3]
    for _ in range(40):
        px += 0.05
        rows.append((px - 0.05, px + 0.02, px - 0.07, px))
    m30 = bars(rows)
    prep = EmaRsiStrategy(EmaRsiParams(long_rsi_max=D(100))).prepare(m30, [])
    sides = [prep.decide(i).side for i in range(len(m30))]
    first = sides.index(Side.LONG)
    assert first > 80
    assert prep.fast[first] > prep.slow[first] and prep.fast[first - 1] <= prep.slow[first - 1]


def test_gap_resets_indicator_warmup() -> None:
    m30 = bars([(80, 80.1, 79.9, 80)] * 120)
    shift = timedelta(hours=5)
    later = [replace(b, open_time=b.open_time + shift, close_time=b.close_time + shift)
             for b in m30[60:]]  # fmt: skip
    gapped = m30[:60] + later
    assert run_length(gapped)[60] == 1
    prep = EmaRsiStrategy().prepare(gapped, [])
    assert prep.decide(61).reason == "DATA_GAP"
    assert prep.slow[60] is None  # EMA restarted after the gap, not carried over


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_decisions_are_prefix_invariant_on_real_data(sid: str, recorded_market: Any) -> None:
    """No look-ahead: the decision for bar i is identical whether or not later bars exist,
    and whatever values those later bars have."""
    m30, h1 = recorded_market.m30, recorded_market.h1
    full = build_strategy(sid).prepare(m30, h1)
    rng = random.Random(7)
    for cut in (600, 1700, 3333, len(m30) - 5):
        t_cut = m30[cut].close_time
        trunc_m30 = m30[: cut + 1]
        trunc_h1 = [b for b in h1 if b.close_time <= t_cut]
        # poison the future: later bars with arbitrary values must not matter
        future = [replace(b, close=b.close * D(str(1 + rng.random())), high=b.high * 3)
                  for b in m30[cut + 1 :]]  # fmt: skip
        poisoned = build_strategy(sid).prepare(trunc_m30 + future, h1)
        trunc = build_strategy(sid).prepare(trunc_m30, trunc_h1)
        for i in range(cut - 300, cut + 1):
            a, b, c = full.decide(i), trunc.decide(i), poisoned.decide(i)
            assert a == b == c, (sid, i)


def test_params_validation_and_hash() -> None:
    with pytest.raises(ValueError):
        EmaRsiParams(fast=50, slow=20).validate()
    with pytest.raises(ValueError):
        BreakoutParams(volume_mult=D(-1)).validate()
    a = params_hash("ema_rsi", "1.0.0", default_params("ema_rsi"))
    b = params_hash("ema_rsi", "1.0.0", EmaRsiParams(fast=21))
    assert a != b and len(a) == 64
    assert a == params_hash("ema_rsi", "1.0.0", EmaRsiParams())
    with pytest.raises(ValueError):
        build_strategy("nope")
    with pytest.raises(TypeError):
        build_strategy("ema_rsi", BreakoutParams())
