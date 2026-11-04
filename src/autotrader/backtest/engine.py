"""Deterministic, event-driven backtest engine for closed OHLC bars (PAPER research only).

Timeline per 30m bar ``k`` (all bars are CLOSED historical bars):

1. funding for every hour ``h`` with ``entry_time < h <= open_time[k]`` is charged;
2. a pending entry decided at the close of an earlier bar is filled at
   ``open[k]`` (never at the signal bar's close);
3. an open position is checked for a gap through the stop/target at ``open[k]``,
   then for stop/target touches inside the bar. If both are inside the same bar
   the configured ambiguity policy decides (default: STOP first);
4. at the close of bar ``k`` equity is marked, risk limits are updated and the
   strategy is asked for a decision using only bars ``<= k``. An accepted
   signal becomes a pending entry for the next contiguous bar.

There is no code path that sends an order anywhere: the engine only produces
records in memory.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from autotrader.backtest.costs import CostModel, FundingMode, floor_size, round_price
from autotrader.strategy.base import Bar, Decision, Prepared, Side
from autotrader.strategy.sessions import ReferenceSession

HOUR = timedelta(hours=1)
ZERO = Decimal(0)


class Ambiguity(StrEnum):
    STOP_FIRST = "STOP_FIRST"
    TARGET_FIRST = "TARGET_FIRST"
    #: the extreme closer to the open is assumed to trade first (sensitivity only)
    OPEN_PROXIMITY = "OPEN_PROXIMITY"


class ExitReason(StrEnum):
    STOP = "STOP"
    STOP_GAP = "STOP_GAP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_GAP = "TAKE_PROFIT_GAP"
    TIME = "TIME"
    END_OF_DATA = "END_OF_DATA"


@dataclass(frozen=True)
class RiskConfig:
    initial_equity: Decimal = Decimal(1000)
    risk_per_trade_pct: Decimal = Decimal("0.25")
    daily_loss_limit_pct: Decimal = Decimal("1.0")
    drawdown_halt_pct: Decimal = Decimal("5.0")
    max_leverage: Decimal = Decimal(2)
    max_entries_per_day: int = 4
    halt_after_consecutive_losses: int = 3
    #: Hyperliquid minimum order value (ASSUMPTION: documented as $10, not re-verified)
    min_notional: Decimal = Decimal(10)

    def validate(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")
        if not ZERO < self.risk_per_trade_pct <= Decimal(2):
            raise ValueError("risk_per_trade_pct must be in (0, 2]")
        if not ZERO < self.daily_loss_limit_pct <= Decimal(20):
            raise ValueError("daily_loss_limit_pct must be in (0, 20]")
        if not ZERO < self.drawdown_halt_pct <= Decimal(50):
            raise ValueError("drawdown_halt_pct must be in (0, 50]")
        if not ZERO < self.max_leverage <= Decimal(5):
            raise ValueError("max_leverage must be in (0, 5]")
        if self.max_entries_per_day < 1 or self.halt_after_consecutive_losses < 1:
            raise ValueError("entry/loss limits must be >= 1")

    def as_dict(self) -> dict[str, Any]:
        return {k: str(v) for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class ExecConfig:
    ambiguity: Ambiguity = Ambiguity.STOP_FIRST
    #: extra bars of delay after the signal (0 = next bar open)
    extra_entry_delay_bars: int = 0
    #: probability that an accepted entry is not filled (seeded, deterministic)
    skip_fill_rate: Decimal = ZERO
    seed: int = 20261104
    #: ``None`` disables the reference-session gate (sensitivity only)
    session: ReferenceSession | None = field(default_factory=ReferenceSession)
    #: signal bar range above this many ATRs is treated as abnormal volatility
    max_signal_range_atr: Decimal = Decimal(4)
    #: |hourly funding| above this blocks entries (abnormal funding)
    max_abs_funding_rate: Decimal = Decimal("0.0005")
    #: fail closed: no recorded funding in the last 2h blocks entries
    require_funding_data: bool = True
    sz_decimals: int = 3

    def as_dict(self) -> dict[str, Any]:
        s = self.session
        return {
            "ambiguity": self.ambiguity.value,
            "extra_entry_delay_bars": self.extra_entry_delay_bars,
            "skip_fill_rate": str(self.skip_fill_rate),
            "seed": self.seed,
            "session_gate": None
            if s is None
            else {
                "edge_buffer_minutes": int(s.edge_buffer.total_seconds() // 60),
                "roll_blackout_business_days": s.roll_blackout,
            },
            "max_signal_range_atr": str(self.max_signal_range_atr),
            "max_abs_funding_rate": str(self.max_abs_funding_rate),
            "require_funding_data": self.require_funding_data,
            "sz_decimals": self.sz_decimals,
        }


@dataclass
class Trade:
    id: int
    side: Side
    signal_bar_open: datetime
    signal_time: datetime
    entry_time: datetime
    entry_ref_price: Decimal
    entry_price: Decimal
    size: Decimal
    stop: Decimal
    target: Decimal
    atr: Decimal
    risk_budget: Decimal
    planned_loss: Decimal
    equity_at_entry: Decimal
    entry_offhours: bool
    rationale: dict[str, str]
    entry_fee: Decimal
    exit_time: datetime | None = None
    exit_bar_open: datetime | None = None
    exit_ref_price: Decimal | None = None
    exit_price: Decimal | None = None
    exit_reason: ExitReason | None = None
    exit_offhours: bool = False
    ambiguous_bar: bool = False
    exit_fee: Decimal = ZERO
    funding: Decimal = ZERO  # positive = paid by us
    funding_hours: int = 0
    funding_missing_hours: int = 0
    spread_cost: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    gross_pnl: Decimal = ZERO
    net_pnl: Decimal = ZERO
    bars_held: int = 0
    crossed_data_gap: bool = False
    mae: Decimal = ZERO  # worst adverse excursion in price units
    mfe: Decimal = ZERO

    @property
    def notional(self) -> Decimal:
        return self.size * self.entry_price

    @property
    def fees(self) -> Decimal:
        return self.entry_fee + self.exit_fee

    @property
    def r_multiple(self) -> Decimal:
        return self.net_pnl / self.planned_loss if self.planned_loss > 0 else ZERO


@dataclass(frozen=True)
class SignalRecord:
    """Audit trail of every strategy signal: accepted (filled) or blocked, with why."""

    signal_time: datetime
    side: Side
    status: str  # FILLED / BLOCKED
    reason: str
    trade_id: int | None
    rationale: dict[str, str]

    @property
    def category(self) -> str:
        return block_category(self.reason)


#: Why a signal did not become a trade. STRATEGY_FILTER blocks are choices of the
#: strategy (e.g. the CME session filter) - xyz:CL itself remains tradable 24/7.
BLOCK_CATEGORIES: dict[str, str] = {
    "FILLED": "FILLED",
    "REFERENCE_SESSION_CLOSED": "STRATEGY_FILTER",
    "REFERENCE_SESSION_EDGE": "STRATEGY_FILTER",
    "REFERENCE_HOLIDAY": "STRATEGY_FILTER",
    "CONTRACT_ROLL_WINDOW": "STRATEGY_FILTER",
    "SESSION_CALENDAR_UNKNOWN": "STRATEGY_FILTER",
    "ABNORMAL_VOLATILITY": "STRATEGY_FILTER",
    "ABNORMAL_FUNDING": "STRATEGY_FILTER",
    "DAILY_LOSS_LIMIT": "RISK",
    "CONSECUTIVE_LOSSES_COOLDOWN": "RISK",
    "MAX_ENTRIES_PER_DAY": "RISK",
    "DRAWDOWN_HALT": "RISK",
    "BANKRUPT": "RISK",
    "SIZE_BELOW_MINIMUM": "RISK",
    "INVALID_STOP": "RISK",
    "NO_NEXT_BAR": "DATA",
    "DATA_GAP": "DATA",
    "FUNDING_DATA_MISSING": "DATA",
    "NO_PLAUSIBLE_EXECUTION": "MARKET",
    "SKIPPED_FILL_SIMULATED": "SIMULATION",
}


def block_category(reason: str) -> str:
    return BLOCK_CATEGORIES.get(reason, "OTHER")


@dataclass(frozen=True)
class EquityPoint:
    time: datetime
    equity: Decimal  # cash + unrealized at bar close
    worst: Decimal  # conservative intrabar mark
    in_position: bool


@dataclass
class BacktestResult:
    trades: list[Trade]
    signals: list[SignalRecord]
    equity: list[EquityPoint]
    events: list[dict[str, str]]
    decision_counts: dict[str, int]
    initial_equity: Decimal
    final_equity: Decimal
    first_bar: datetime | None
    last_bar: datetime | None
    trade_window: tuple[datetime | None, datetime | None]
    bars_in_window: int
    halted: str | None = None


@dataclass
class _Pending:
    fill_index: int
    signal_index: int
    decision: Decision


class FundingBook:
    """Hourly funding rates keyed by the hour (UTC). Missing hours are explicit."""

    def __init__(self, rates: Mapping[datetime, Decimal]) -> None:
        self._rates = {_floor_hour(t): r for t, r in rates.items()}
        times = sorted(self._rates)
        self.first = times[0] if times else None
        self.last = times[-1] if times else None
        self._sorted = times

    def rate(self, hour: datetime) -> Decimal | None:
        return self._rates.get(hour)

    def latest_at_or_before(self, t: datetime) -> Decimal | None:
        import bisect

        i = bisect.bisect_right(self._sorted, t) - 1
        if i < 0 or t - self._sorted[i] > 2 * HOUR:
            return None
        return self._rates[self._sorted[i]]

    def __len__(self) -> int:
        return len(self._rates)


def _floor_hour(t: datetime) -> datetime:
    t = t.astimezone(UTC)
    return t.replace(minute=0, second=0, microsecond=0)


def _next_utc_midnight(t: datetime) -> datetime:
    d = t.astimezone(UTC).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


class Engine:
    def __init__(
        self,
        bars: Sequence[Bar],
        prepared: Prepared,
        *,
        stop_atr: Decimal,
        tp_r: Decimal,
        max_hold_bars: int | None,
        costs: CostModel,
        risk: RiskConfig,
        execution: ExecConfig,
        funding: FundingBook,
        trade_start: datetime | None = None,
        trade_end: datetime | None = None,
    ) -> None:
        costs.validate()
        risk.validate()
        self.bars = bars
        self.prep = prepared
        self.stop_atr, self.tp_r, self.max_hold = stop_atr, tp_r, max_hold_bars
        self.costs, self.risk, self.ex = costs, risk, execution
        self.funding = funding
        self.trade_start, self.trade_end = trade_start, trade_end
        self.rng = random.Random(execution.seed)  # noqa: S311 - seeded simulation, not crypto

        self.cash = risk.initial_equity
        self.pos: Trade | None = None
        self.pending: _Pending | None = None
        self.pending_exit_at: int | None = None
        self.trades: list[Trade] = []
        self.signals: list[SignalRecord] = []
        self.equity: list[EquityPoint] = []
        self.events: list[dict[str, str]] = []
        self.counts: Counter[str] = Counter()
        self.hwm = risk.initial_equity
        self.halted: str | None = None
        self.day: Any = None
        self.day_start_equity = risk.initial_equity
        self.daily_halt = False
        self.cooldown_until: datetime | None = None
        self.consecutive_losses = 0
        self.entries_by_day: Counter[Any] = Counter()
        self.last_funding_hour: datetime | None = None
        self.bars_in_window = 0

    # ----------------------------------------------------------------- helpers
    def _in_window(self, t: datetime) -> bool:
        if self.trade_start is not None and t < self.trade_start:
            return False
        return not (self.trade_end is not None and t >= self.trade_end)

    def _mult(self, at: datetime) -> Decimal:
        s = self.ex.session or ReferenceSession()
        return Decimal(1) if s.reference_trading(at) else self.costs.offhours_multiplier

    def _market_fill(self, ref: Decimal, side_sign: int, at: datetime) -> tuple[Decimal, bool]:
        """Fill for a market order: +sign = buy. Returns (price, offhours)."""
        mult = self._mult(at)
        adj = (self.costs.half_spread_rate + self.costs.slippage_rate) * mult
        raw = ref * (Decimal(1) + side_sign * adj)
        mode = "up" if side_sign > 0 else "down"
        return round_price(raw, self.ex.sz_decimals, mode), mult != 1

    def _event(self, t: datetime, kind: str, detail: str) -> None:
        self.events.append({"time": t.isoformat(), "event": kind, "detail": detail})

    def _mark(self, close: Decimal) -> Decimal:
        if self.pos is None:
            return self.cash
        p = self.pos
        return self.cash + p.side.sign * (close - p.entry_price) * p.size

    # ----------------------------------------------------------------- funding
    def _accrue_funding(self, upto: datetime, price: Decimal) -> None:
        p = self.pos
        if p is None:
            return
        h = _floor_hour(upto)
        start = self.last_funding_hour or _floor_hour(p.entry_time)
        hour = start + HOUR
        while hour <= h:
            if hour > p.entry_time:
                self._charge_funding_hour(p, hour, price)
            hour += HOUR
        self.last_funding_hour = max(h, start)

    def _charge_funding_hour(self, p: Trade, hour: datetime, price: Decimal) -> None:
        c = self.costs
        notional = p.size * price
        if c.funding_mode is FundingMode.ZERO:
            cost = ZERO
        else:
            rate = self.funding.rate(hour)
            if rate is None:
                p.funding_missing_hours += 1
                cost = notional * c.missing_funding_rate
            elif c.funding_mode is FundingMode.ADVERSE:
                cost = notional * abs(rate) * c.funding_multiplier
            else:
                # positive rate: longs pay shorts
                cost = p.side.sign * notional * rate * c.funding_multiplier
        p.funding += cost
        p.funding_hours += 1
        self.cash -= cost

    # ----------------------------------------------------------------- entries
    def _try_fill(self, k: int) -> None:
        pend = self.pending
        assert pend is not None
        self.pending = None
        bar = self.bars[k]
        d = pend.decision
        assert d.side is not None and d.atr is not None
        sig_bar = self.bars[pend.signal_index]
        side = d.side
        equity = self.cash
        fill, off = self._market_fill(bar.open, side.sign, bar.open_time)
        sz = self.ex.sz_decimals
        raw_stop = fill - side.sign * self.stop_atr * d.atr
        if raw_stop <= 0 or d.atr <= 0:
            return self._block(sig_bar, d, "INVALID_STOP")
        stop = round_price(raw_stop, sz)
        risk_px = abs(fill - stop)
        if (
            risk_px <= 0
            or (side is Side.LONG and stop >= fill)
            or (side is Side.SHORT and stop <= fill)
        ):
            return self._block(sig_bar, d, "INVALID_STOP")
        target = round_price(fill + side.sign * self.tp_r * risk_px, sz)
        # conservative estimate of the stop fill: worst (off-hours) multiplier
        worst_adj = (self.costs.half_spread_rate + self.costs.slippage_rate) * max(
            self.costs.offhours_multiplier, Decimal(1)
        )
        est_stop_fill = stop * (Decimal(1) - side.sign * worst_adj)
        fee = self.costs.taker_fee_rate
        per_unit = abs(fill - est_stop_fill) + fee * fill + fee * est_stop_fill
        budget = equity * self.risk.risk_per_trade_pct / Decimal(100)
        size = floor_size(budget / per_unit, sz)
        cap = floor_size(self.risk.max_leverage * equity / fill, sz)
        size = min(size, cap)
        if size <= 0 or size * fill < self.risk.min_notional:
            return self._block(sig_bar, d, "SIZE_BELOW_MINIMUM")
        planned = size * per_unit
        entry_fee = fee * fill * size
        self.cash -= entry_fee
        t = Trade(
            id=len(self.trades) + 1,
            side=side,
            signal_bar_open=sig_bar.open_time,
            signal_time=sig_bar.close_time,
            entry_time=bar.open_time,
            entry_ref_price=bar.open,
            entry_price=fill,
            size=size,
            stop=stop,
            target=target,
            atr=d.atr,
            risk_budget=budget,
            planned_loss=planned,
            equity_at_entry=equity,
            entry_offhours=off,
            rationale=d.rationale,
            entry_fee=entry_fee,
        )
        self.trades.append(t)
        self.pos = t
        self.last_funding_hour = _floor_hour(bar.open_time)
        self.entries_by_day[bar.open_time.astimezone(UTC).date()] += 1
        self.signals.append(
            SignalRecord(sig_bar.close_time, side, "FILLED", "FILLED", t.id, d.rationale)
        )
        self.counts["FILLED"] += 1

    def _block(self, sig_bar: Bar, d: Decision, reason: str) -> None:
        assert d.side is not None
        self.signals.append(
            SignalRecord(sig_bar.close_time, d.side, "BLOCKED", reason, None, d.rationale)
        )
        self.counts[f"BLOCKED:{reason}"] += 1

    def _gate(self, i: int, d: Decision) -> tuple[str | None, int | None]:
        """Risk/execution gates for a signal at the close of bar i. Returns (block, fill_k)."""
        bar = self.bars[i]
        t = bar.close_time
        assert d.atr is not None
        if self.halted:
            return self.halted, None
        k = i + 1 + self.ex.extra_entry_delay_bars
        expected_open = t + (bar.close_time - bar.open_time) * self.ex.extra_entry_delay_bars
        # the last bar of the data/window is reserved for the forced END_OF_DATA exit
        if k >= len(self.bars) - 1:
            return "NO_NEXT_BAR", None
        if self.bars[k].open_time != expected_open:
            return "DATA_GAP", None
        entry_at = self.bars[k].open_time
        s = self.ex.session
        if s is not None:
            reason = s.entry_block_reason(entry_at) or (
                "REFERENCE_SESSION_CLOSED" if not s.is_open(bar.open_time) else None
            )
            if reason:
                return reason, None
        if self.daily_halt:
            return "DAILY_LOSS_LIMIT", None
        if self.cooldown_until is not None and entry_at < self.cooldown_until:
            return "CONSECUTIVE_LOSSES_COOLDOWN", None
        if self.entries_by_day[entry_at.astimezone(UTC).date()] >= self.risk.max_entries_per_day:
            return "MAX_ENTRIES_PER_DAY", None
        if bar.high - bar.low > self.ex.max_signal_range_atr * d.atr:
            return "ABNORMAL_VOLATILITY", None
        # no trades in the signal bar (known at its close) -> no evidence of a live market
        if bar.volume <= 0:
            return "NO_PLAUSIBLE_EXECUTION", None
        fr = self.funding.latest_at_or_before(t)
        if fr is None and self.ex.require_funding_data:
            return "FUNDING_DATA_MISSING", None
        if fr is not None and abs(fr) > self.ex.max_abs_funding_rate:
            return "ABNORMAL_FUNDING", None
        if self.ex.skip_fill_rate > 0 and Decimal(str(self.rng.random())) < self.ex.skip_fill_rate:
            return "SKIPPED_FILL_SIMULATED", None
        return None, k

    # ----------------------------------------------------------------- exits
    def _close(
        self,
        bar: Bar,
        at: datetime,
        ref: Decimal,
        reason: ExitReason,
        *,
        market: bool,
        ambiguous: bool = False,
    ) -> None:
        p = self.pos
        assert p is not None
        if market:
            fill, off = self._market_fill(ref, -p.side.sign, at)
        else:
            fill, off = ref, not (self.ex.session or ReferenceSession()).reference_trading(at)
        exit_fee = self.costs.taker_fee_rate * fill * p.size
        self.cash += p.side.sign * (fill - p.entry_price) * p.size - exit_fee
        p.exit_time, p.exit_bar_open, p.exit_ref_price = at, bar.open_time, ref
        p.exit_price, p.exit_reason, p.exit_offhours = fill, reason, off
        p.ambiguous_bar = ambiguous
        p.exit_fee = exit_fee
        p.gross_pnl = p.side.sign * (ref - p.entry_ref_price) * p.size
        fill_pnl = p.side.sign * (fill - p.entry_price) * p.size
        hs = self.costs.half_spread_rate
        spread = hs * self._mult(p.entry_time) * p.entry_ref_price * p.size
        if market:
            spread += hs * self._mult(at) * ref * p.size
        p.spread_cost = spread
        # everything else between reference and fill prices (incl. tick rounding)
        p.slippage_cost = p.gross_pnl - fill_pnl - spread
        p.net_pnl = fill_pnl - p.entry_fee - exit_fee - p.funding
        self.pos = None
        self.pending_exit_at = None
        self.last_funding_hour = None
        if p.net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.risk.halt_after_consecutive_losses:
                self.cooldown_until = _next_utc_midnight(at)
                self._event(
                    at,
                    "CONSECUTIVE_LOSSES",
                    f"{self.consecutive_losses} losses in a row; entries paused until "
                    f"{self.cooldown_until.isoformat()}",
                )
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    def _process_exits(self, k: int) -> None:
        p = self.pos
        if p is None:
            return
        bar = self.bars[k]
        if self.pending_exit_at == k:
            return self._close(bar, bar.open_time, bar.open, ExitReason.TIME, market=True)
        sign = p.side.sign
        first_bar = bar.open_time == p.entry_time
        if not first_bar:
            if (sign > 0 and bar.open <= p.stop) or (sign < 0 and bar.open >= p.stop):
                return self._close(bar, bar.open_time, bar.open, ExitReason.STOP_GAP, market=True)
            if (sign > 0 and bar.open >= p.target) or (sign < 0 and bar.open <= p.target):
                return self._close(
                    bar, bar.open_time, p.target, ExitReason.TAKE_PROFIT_GAP, market=False
                )
        hit_stop = bar.low <= p.stop if sign > 0 else bar.high >= p.stop
        # a target needs a trade THROUGH it; a touch is not enough
        hit_tp = bar.high > p.target if sign > 0 else bar.low < p.target
        mid = bar.open_time + (bar.close_time - bar.open_time) / 2
        if hit_stop and hit_tp:
            amb = self.ex.ambiguity
            if amb is Ambiguity.OPEN_PROXIMITY:
                toward_tp = (bar.high - bar.open) if sign > 0 else (bar.open - bar.low)
                toward_sl = (bar.open - bar.low) if sign > 0 else (bar.high - bar.open)
                stop_first = toward_sl <= toward_tp
            else:
                stop_first = amb is Ambiguity.STOP_FIRST
            if stop_first:
                return self._close(bar, mid, p.stop, ExitReason.STOP, market=True, ambiguous=True)
            return self._close(
                bar, mid, p.target, ExitReason.TAKE_PROFIT, market=False, ambiguous=True
            )
        if hit_stop:
            return self._close(bar, mid, p.stop, ExitReason.STOP, market=True)
        if hit_tp:
            return self._close(bar, mid, p.target, ExitReason.TAKE_PROFIT, market=False)
        return None

    # ----------------------------------------------------------------- main loop
    def run(self) -> BacktestResult:
        bars = self.bars
        n = len(bars)
        for k in range(n):
            bar = bars[k]
            # daily accounting resets at 00:00 UTC
            day = bar.open_time.astimezone(UTC).date()
            if day != self.day:
                self.day = day
                self.day_start_equity = self._mark(bars[k - 1].close) if k else self.cash
                self.daily_halt = False
            if self.pos is not None:
                gap = bars[k - 1].close_time != bar.open_time
                if gap:
                    self.pos.crossed_data_gap = True
                self._accrue_funding(bar.open_time, bars[k - 1].close)
            if self.pending is not None and self.pending.fill_index == k:
                self._try_fill(k)
            if self.pos is not None:
                self._update_excursions(bar)
                self._process_exits(k)
            if self.pos is not None:
                self.pos.bars_held += 1
            last_in_window = k == n - 1
            if self.pos is not None and last_in_window:
                self._accrue_funding(bar.close_time, bar.close)
                self._close(bar, bar.close_time, bar.close, ExitReason.END_OF_DATA, market=True)
            self._mark_and_limits(bar)
            if self._in_window(bar.close_time - timedelta(microseconds=1)):
                self.bars_in_window += 1
            held = self.pos.bars_held if self.pos is not None else 0
            if self.max_hold and held >= self.max_hold and k + 1 < n:
                self.pending_exit_at = k + 1
            if self.pos is None and self.pending is None and not last_in_window:
                self._decide(k)
        final = self.cash
        return BacktestResult(
            trades=self.trades,
            signals=self.signals,
            equity=self.equity,
            events=self.events,
            decision_counts=dict(sorted(self.counts.items())),
            initial_equity=self.risk.initial_equity,
            final_equity=final,
            first_bar=bars[0].open_time if bars else None,
            last_bar=bars[-1].open_time if bars else None,
            trade_window=(self.trade_start, self.trade_end),
            bars_in_window=self.bars_in_window,
            halted=self.halted,
        )

    def _update_excursions(self, bar: Bar) -> None:
        p = self.pos
        assert p is not None
        if p.side is Side.LONG:
            p.mae = max(p.mae, p.entry_price - bar.low)
            p.mfe = max(p.mfe, bar.high - p.entry_price)
        else:
            p.mae = max(p.mae, bar.high - p.entry_price)
            p.mfe = max(p.mfe, p.entry_price - bar.low)

    def _mark_and_limits(self, bar: Bar) -> None:
        eq = self._mark(bar.close)
        if self.pos is not None:
            worst_px = bar.low if self.pos.side is Side.LONG else bar.high
            worst = min(eq, self._mark(worst_px))
        else:
            worst = eq
        if self._in_window(bar.close_time - timedelta(microseconds=1)):
            self.equity.append(EquityPoint(bar.close_time, eq, worst, self.pos is not None))
        self.hwm = max(self.hwm, eq)
        if eq <= 0 and self.halted is None:
            self.halted = "BANKRUPT"
            self._event(bar.close_time, "BANKRUPT", f"equity {eq}")
        dd_limit = self.hwm * (Decimal(1) - self.risk.drawdown_halt_pct / Decimal(100))
        if self.halted is None and eq <= dd_limit:
            self.halted = "DRAWDOWN_HALT"
            self._event(
                bar.close_time,
                "DRAWDOWN_HALT",
                f"equity {eq:.2f} <= {dd_limit:.2f} ({self.risk.drawdown_halt_pct}% below "
                f"high-water {self.hwm:.2f}); no new entries for the rest of the run",
            )
        day_limit = self.day_start_equity * (
            Decimal(1) - self.risk.daily_loss_limit_pct / Decimal(100)
        )
        if not self.daily_halt and worst <= day_limit:
            self.daily_halt = True
            self._event(
                bar.close_time,
                "DAILY_LOSS_LIMIT",
                f"conservative equity {worst:.2f} <= {day_limit:.2f}; entries blocked until "
                "00:00 UTC",
            )

    def _decide(self, i: int) -> None:
        bar = self.bars[i]
        if not self._in_window(bar.close_time):
            return
        d = self.prep.decide(i)
        self.counts[d.reason] += 1
        if d.side is None:
            return
        block, k = self._gate(i, d)
        if block is not None:
            return self._block(bar, d, block)
        assert k is not None
        self.pending = _Pending(fill_index=k, signal_index=i, decision=d)
        return None
