"""Indicator correctness (hand-computed values, independent float reference) and causality."""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

import pytest

from autotrader.strategy import indicators as ind

D = Decimal


def test_sma_and_ema_hand_values() -> None:
    v = [D(x) for x in (1, 2, 3, 4, 5)]
    assert ind.sma(v, 3) == [None, None, D(2), D(3), D(4)]
    e = ind.ema(v, 3)  # alpha = 0.5, seed = mean(1,2,3) = 2
    assert e[:2] == [None, None]
    assert e[2] == D(2)
    assert e[3] == D(3)  # 2 + .5*(4-2)
    assert e[4] == D(4)  # 3 + .5*(5-3)


def test_ema_shorter_than_window_is_all_none() -> None:
    assert ind.ema([D(1), D(2)], 3) == [None, None]


def test_rsi_extremes_and_flat() -> None:
    up = [D(i) for i in range(30)]
    down = [D(100 - i) for i in range(30)]
    flat = [D(5)] * 30
    assert ind.rsi(up, 14)[-1] == 100
    assert ind.rsi(down, 14)[-1] == 0
    assert ind.rsi(flat, 14)[-1] == 50
    assert ind.rsi(up, 14)[13] is None and ind.rsi(up, 14)[14] is not None


def _rsi_float(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    lo = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + lo[i - 1]) / n
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def test_rsi_and_atr_match_independent_float_reference(snapshot_30m: Any) -> None:
    closes = [D(c["c"]) for c in snapshot_30m]
    highs = [D(c["h"]) for c in snapshot_30m]
    lows = [D(c["l"]) for c in snapshot_30m]
    ref = _rsi_float([float(x) for x in closes], 14)
    got = ind.rsi(closes, 14)
    for a, b in zip(got, ref, strict=True):
        assert (a is None) == (b is None)
        if a is not None and b is not None:
            assert abs(float(a) - b) < 1e-9
    # ATR: Wilder smoothing of true range, seeded by the simple mean
    tr = ind.true_range(highs, lows, closes)
    atr = ind.atr(highs, lows, closes, 14)
    prev = sum(float(x) for x in tr[:14]) / 14
    assert abs(float(atr[13]) - prev) < 1e-12  # type: ignore[arg-type]
    for i in range(14, len(tr)):
        prev = (prev * 13 + float(tr[i])) / 14
        assert abs(float(atr[i]) - prev) < 1e-9  # type: ignore[arg-type]


def test_true_range_uses_previous_close() -> None:
    tr = ind.true_range([D(10), D(12)], [D(9), D(11)], [D(9.5), D(11.5)])
    assert tr == [D(1), D("2.5")]  # gap up: high 12 - prev close 9.5


def test_prior_window_stats_exclude_current_bar() -> None:
    v = [D(x) for x in (5, 1, 9, 3, 100)]
    assert ind.prior_max(v, 3)[4] == D(9)  # 100 (current) is excluded
    assert ind.prior_min(v, 3)[4] == D(1)
    assert ind.prior_median(v, 3)[4] == D(3)
    assert ind.prior_median(v, 4)[4] == D(4)  # (3+5)/2
    assert ind.prior_max(v, 3)[2] is None


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_invalid_window_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        ind.ema([D(1)], bad)


def test_all_indicators_are_causal(snapshot_30m: Any) -> None:
    """Prefix invariance: value i never changes when later data is added or altered."""
    closes = [D(c["c"]) for c in snapshot_30m]
    highs = [D(c["h"]) for c in snapshot_30m]
    lows = [D(c["l"]) for c in snapshot_30m]
    rng = random.Random(3)
    full = {
        "ema": ind.ema(closes, 20),
        "rsi": ind.rsi(closes, 14),
        "atr": ind.atr(highs, lows, closes, 14),
        "pmax": ind.prior_max(highs, 20),
        "pmed": ind.prior_median(closes, 20),
    }
    for cut in (25, 60, 120, 199):
        fut = [x * D(str(1 + rng.random())) for x in closes[cut:]]
        c2 = closes[:cut] + fut
        h2 = highs[:cut] + [x * 2 for x in fut]
        l2 = lows[:cut] + [x / 2 for x in fut]
        alt = {
            "ema": ind.ema(c2, 20),
            "rsi": ind.rsi(c2, 14),
            "atr": ind.atr(h2, l2, c2, 14),
            "pmax": ind.prior_max(h2, 20),
            "pmed": ind.prior_median(c2, 20),
        }
        for k in full:
            assert full[k][:cut] == alt[k][:cut], k
