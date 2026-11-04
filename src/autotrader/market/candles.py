"""Candle model, parsing and per-candle validation (pure logic, no I/O).

Hyperliquid candle payloads (REST ``candleSnapshot`` and WS ``candle``) look like::

    {"t": open_ms, "T": close_ms_inclusive, "s": "xyz:CL", "i": "30m",
     "o": "96.37", "c": "96.18", "h": "96.56", "l": "96.11", "v": "3546.51", "n": 303}

``T`` is the last millisecond of the bar (``t + interval - 1``). We store
``close_time = t + interval`` (exclusive end) and decide "closed" from that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final


class Interval(StrEnum):
    M30 = "30m"
    H1 = "1h"

    @property
    def ms(self) -> int:
        return _INTERVAL_MS[self]

    @property
    def delta(self) -> timedelta:
        return timedelta(milliseconds=self.ms)


_INTERVAL_MS: Final[dict[Interval, int]] = {Interval.M30: 30 * 60_000, Interval.H1: 60 * 60_000}

#: A bar counts as closed only after its end plus this grace period has passed on
#: our clock. Protects against using a bar that the exchange is still updating.
CLOSE_GRACE: Final = timedelta(seconds=5)

# Same sanity bounds as market discovery (ADR-011): outside them the bar is rejected.
PRICE_MIN: Final = Decimal("1e-8")
PRICE_MAX: Final = Decimal("1e9")
VOLUME_MAX: Final = Decimal("1e15")


class CandleRejection(StrEnum):
    MALFORMED = "MALFORMED"
    WRONG_MARKET = "WRONG_MARKET"
    MISALIGNED_TIME = "MISALIGNED_TIME"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    INVALID_OHLC = "INVALID_OHLC"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"


@dataclass(frozen=True, slots=True)
class Candle:
    coin: str
    interval: Interval
    open_time: datetime
    close_time: datetime  # exclusive end = open_time + interval
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int | None

    def is_closed_at(self, now: datetime) -> bool:
        return now >= self.close_time + CLOSE_GRACE

    def same_values(self, other: Candle) -> bool:
        return (
            self.open == other.open
            and self.high == other.high
            and self.low == other.low
            and self.close == other.close
            and self.volume == other.volume
            and self.trades == other.trades
        )


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: CandleRejection
    detail: str
    raw: Any
    open_time: datetime | None = None


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def dt_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dec(raw: Any, field: str) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise ValueError(f"{field} must be a numeric string")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a number: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{field} is not finite: {raw!r}")
    return value


def parse_candle(raw: Any, *, coin: str, interval: Interval) -> Candle | Rejected:
    """Parse and validate one raw candle. Never raises; invalid bars are returned
    as :class:`Rejected` so they can be recorded as data-quality issues."""
    if not isinstance(raw, dict):
        return Rejected(CandleRejection.MALFORMED, "candle is not an object", raw)
    try:
        t = raw["t"]
        if isinstance(t, bool) or not isinstance(t, int):
            raise ValueError("t must be an integer (ms)")
        open_time = ms_to_dt(t)
        o, h, low, c = (_dec(raw[k], k) for k in ("o", "h", "l", "c"))
        v = _dec(raw["v"], "v")
        n_raw = raw.get("n")
        if n_raw is not None and (
            isinstance(n_raw, bool) or not isinstance(n_raw, int) or not 0 <= n_raw <= 2**31 - 1
        ):
            # Strict like prices: no floats/bools/negatives, must fit the INTEGER column.
            raise ValueError(f"n must be a non-negative integer: {n_raw!r}")
        trades = n_raw
        symbol, iv = raw.get("s"), raw.get("i")
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return Rejected(CandleRejection.MALFORMED, str(exc), raw)

    if symbol != coin or iv != interval.value:
        return Rejected(
            CandleRejection.WRONG_MARKET, f"got {symbol}/{iv}, want {coin}/{interval}", raw
        )
    if t % interval.ms != 0:
        return Rejected(CandleRejection.MISALIGNED_TIME, f"t={t} not on bar boundary", raw)
    big_t = raw.get("T")
    if big_t is not None and big_t != t + interval.ms - 1:
        return Rejected(CandleRejection.MISALIGNED_TIME, f"T={big_t} != t+interval-1", raw)

    if min(o, h, low, c) <= 0:
        return Rejected(CandleRejection.NON_POSITIVE_PRICE, "price <= 0", raw, open_time)
    if max(o, h, low, c) > PRICE_MAX or min(o, h, low, c) < PRICE_MIN or v > VOLUME_MAX:
        return Rejected(CandleRejection.OUT_OF_RANGE, "value outside sanity range", raw, open_time)
    if v < 0:
        return Rejected(CandleRejection.NEGATIVE_VOLUME, f"volume={v}", raw, open_time)
    if h < max(o, c) or low > min(o, c) or h < low:
        return Rejected(CandleRejection.INVALID_OHLC, f"o={o} h={h} l={low} c={c}", raw, open_time)
    return Candle(
        coin=coin,
        interval=interval,
        open_time=open_time,
        close_time=open_time + interval.delta,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        trades=trades,
    )
