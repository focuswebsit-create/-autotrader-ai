"""Causal technical indicators on Decimal series (pure functions, no I/O).

Every function returns a list aligned with its input where element ``i`` only
depends on inputs ``0..i`` (never on later values). ``None`` means "not warmed
up yet". This causality is what makes the indicators safe to share between the
historical backtest and the forward paper engine; it is asserted by the
prefix-invariance tests in ``tests/unit/test_indicators.py``.

Definitions (documented so results are reproducible):

* EMA(n): seeded with the simple mean of the first ``n`` values, then
  ``ema = prev + alpha * (x - prev)`` with ``alpha = 2 / (n + 1)``.
* RSI(n): Wilder. First average gain/loss = simple mean of the first ``n``
  changes, then ``avg = (prev * (n - 1) + x) / n``. RSI = 100 when avg loss = 0.
* ATR(n): Wilder smoothing of True Range; the first bar's TR is ``high - low``.
* ``prior_max`` / ``prior_min`` / ``prior_median``: statistics of the ``n``
  values BEFORE ``i`` (excluding ``i`` itself), as the blueprint requires for
  breakout levels and volume baselines.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

_PREC = 34  # decimal digits for indicator arithmetic (deterministic on every platform)

Series = list[Decimal | None]


def _check(n: int) -> None:
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"window must be a positive integer, got {n!r}")


def sma(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    with localcontext() as ctx:
        ctx.prec = _PREC
        running = Decimal(0)
        for i, v in enumerate(values):
            running += v
            if i >= n:
                running -= values[i - n]
            if i >= n - 1:
                out[i] = running / n
    return out


def ema(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    with localcontext() as ctx:
        ctx.prec = _PREC
        alpha = Decimal(2) / Decimal(n + 1)
        prev = sum(values[:n], Decimal(0)) / n
        out[n - 1] = prev
        for i in range(n, len(values)):
            prev = prev + alpha * (values[i] - prev)
            out[i] = prev
    return out


def rsi(closes: Sequence[Decimal], n: int) -> Series:
    _check(n)
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    with localcontext() as ctx:
        ctx.prec = _PREC
        gains = [Decimal(0)] * len(closes)
        losses = [Decimal(0)] * len(closes)
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains[i] = d if d > 0 else Decimal(0)
            losses[i] = -d if d < 0 else Decimal(0)
        avg_g = sum(gains[1 : n + 1], Decimal(0)) / n
        avg_l = sum(losses[1 : n + 1], Decimal(0)) / n

        def value(g: Decimal, loss: Decimal) -> Decimal:
            if loss == 0:
                return Decimal(100) if g > 0 else Decimal(50)
            return Decimal(100) - Decimal(100) / (Decimal(1) + g / loss)

        out[n] = value(avg_g, avg_l)
        for i in range(n + 1, len(closes)):
            avg_g = (avg_g * (n - 1) + gains[i]) / n
            avg_l = (avg_l * (n - 1) + losses[i]) / n
            out[i] = value(avg_g, avg_l)
    return out


def true_range(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal]
) -> list[Decimal]:
    out: list[Decimal] = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], n: int
) -> Series:
    _check(n)
    tr = true_range(highs, lows, closes)
    out: Series = [None] * len(tr)
    if len(tr) < n:
        return out
    with localcontext() as ctx:
        ctx.prec = _PREC
        prev = sum(tr[:n], Decimal(0)) / n
        out[n - 1] = prev
        for i in range(n, len(tr)):
            prev = (prev * (n - 1) + tr[i]) / n
            out[i] = prev
    return out


def prior_max(values: Sequence[Decimal], n: int) -> Series:
    """Max of values[i-n .. i-1] (the current value is excluded)."""
    _check(n)
    return [max(values[i - n : i]) if i >= n else None for i in range(len(values))]


def prior_min(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    return [min(values[i - n : i]) if i >= n else None for i in range(len(values))]


def _median(window: Sequence[Decimal]) -> Decimal:
    s = sorted(window)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (s[mid - 1] + s[mid]) / 2


def prior_median(values: Sequence[Decimal], n: int) -> Series:
    _check(n)
    return [_median(values[i - n : i]) if i >= n else None for i in range(len(values))]
