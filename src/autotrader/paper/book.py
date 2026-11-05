"""Live L2 order book (public Hyperliquid ``l2Book``) and conservative fill simulation.

A simulated buy walks the asks, a sell walks the bids, level by level, using the
sizes the exchange actually showed. Then an extra adverse slippage buffer is added
and the price is rounded to the market tick against us. No fill uses a price the
book didn't offer at that moment, and nothing is filled later "in hindsight".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from autotrader.backtest.costs import floor_size, round_price

Level = tuple[Decimal, Decimal]  # (price, size)


@dataclass(frozen=True)
class L2Book:
    coin: str
    time: datetime
    bids: tuple[Level, ...]  # best first (descending price)
    asks: tuple[Level, ...]  # best first (ascending price)

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0][0]

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0][0]

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.best_ask - self.best_bid) / self.mid * Decimal(10_000)

    def age_s(self, now: datetime) -> float:
        return (now - self.time).total_seconds()

    def snapshot(self, levels: int = 5) -> dict[str, Any]:
        """Compact copy stored with every simulated order (audit of the fill)."""
        return {
            "time": self.time.isoformat(),
            "bids": [[str(p), str(s)] for p, s in self.bids[:levels]],
            "asks": [[str(p), str(s)] for p, s in self.asks[:levels]],
        }


def _levels(raw: Any) -> tuple[Level, ...]:
    out: list[Level] = []
    for lvl in raw:
        if not isinstance(lvl, dict):
            raise ValueError("level is not an object")
        px, sz = Decimal(str(lvl["px"])), Decimal(str(lvl["sz"]))
        if not (px.is_finite() and sz.is_finite()) or px <= 0 or sz <= 0:
            raise ValueError("non-positive level")
        out.append((px, sz))
    return tuple(out)


def parse_l2(raw: Any, coin: str) -> L2Book | None:
    """Strict parse; returns None for anything malformed, empty or crossed."""
    try:
        if not isinstance(raw, dict) or raw.get("coin") != coin:
            return None
        t = raw["time"]
        if isinstance(t, bool) or not isinstance(t, int):
            return None
        bids_raw, asks_raw = raw["levels"]
        bids, asks = _levels(bids_raw), _levels(asks_raw)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None
    if not bids or not asks:
        return None
    if any(bids[i][0] <= bids[i + 1][0] for i in range(len(bids) - 1)):
        return None
    if any(asks[i][0] >= asks[i + 1][0] for i in range(len(asks) - 1)):
        return None
    if bids[0][0] >= asks[0][0]:
        return None  # crossed / locked book: never trade on it
    return L2Book(coin, datetime.fromtimestamp(t / 1000, tz=UTC), bids, asks)


@dataclass(frozen=True)
class Fill:
    filled: Decimal
    avg_price: Decimal | None  # after extra slippage and adverse tick rounding
    touch: Decimal  # best price on the side we took
    levels_used: int


def walk(
    book: L2Book,
    side: str,
    size: Decimal,
    *,
    extra_slippage: Decimal,
    sz_decimals: int,
    max_move: Decimal | None = None,
) -> Fill:
    """Take liquidity: BUY walks asks, SELL walks bids. ``max_move`` caps how far from
    the touch the walk may go (partial fill beyond it)."""
    levels = book.asks if side == "BUY" else book.bids
    touch = levels[0][0]
    limit = None
    if max_move is not None:
        limit = touch * (1 + max_move) if side == "BUY" else touch * (1 - max_move)
    remaining = floor_size(size, sz_decimals)
    got = Decimal(0)
    cost = Decimal(0)
    used = 0
    for px, avail in levels:
        if remaining <= 0:
            break
        if limit is not None and (px > limit if side == "BUY" else px < limit):
            break
        take = min(avail, remaining)
        got += take
        cost += take * px
        remaining -= take
        used += 1
    got = floor_size(got, sz_decimals)
    if got <= 0:
        return Fill(Decimal(0), None, touch, used)
    vwap = cost / got
    if side == "BUY":
        price = round_price(vwap * (1 + extra_slippage), sz_decimals, "up")
    else:
        price = round_price(vwap * (1 - extra_slippage), sz_decimals, "down")
    return Fill(got, price, touch, used)


def depth_at_or_better(book: L2Book, side: str, price: Decimal) -> Decimal:
    """Size a resting limit could have filled against right now (SELL >= price on bids,
    BUY <= price on asks)."""
    if side == "SELL":
        return sum((s for p, s in book.bids if p >= price), Decimal(0))
    return sum((s for p, s in book.asks if p <= price), Decimal(0))
