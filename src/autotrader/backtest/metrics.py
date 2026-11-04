"""Performance statistics. Money stays Decimal; statistics use float where only
an estimate is meaningful (bootstrap, Sharpe). Everything is deterministic."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

from autotrader.backtest.engine import BacktestResult, EquityPoint, Trade

BOOTSTRAP_SAMPLES = 4000
BOOTSTRAP_SEED = 1729


def _s(d: Decimal, places: int = 4) -> str:
    return str(d.quantize(Decimal(1).scaleb(-places)))


def max_drawdown(points: Sequence[EquityPoint], initial: Decimal) -> tuple[Decimal, Decimal]:
    """(max drawdown in USDC, in % of the high-water mark) on the conservative mark."""
    hwm = initial
    worst_abs = Decimal(0)
    worst_pct = Decimal(0)
    for p in points:
        dd = hwm - p.worst
        if dd > worst_abs:
            worst_abs = dd
        if hwm > 0 and dd / hwm > worst_pct:
            worst_pct = dd / hwm
        hwm = max(hwm, p.equity)
    return worst_abs, worst_pct * 100


def bootstrap_mean_ci(
    values: Sequence[float], *, samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float] | None:
    """95% percentile bootstrap CI of the mean (None for fewer than 2 values)."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - deterministic statistics
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(samples))
    return means[int(0.025 * samples)], means[int(0.975 * samples) - 1]


def _daily_returns(points: Sequence[EquityPoint], initial: Decimal) -> list[float]:
    by_day: dict[Any, Decimal] = {}
    for p in points:
        by_day[(p.time - timedelta(microseconds=1)).astimezone(UTC).date()] = p.equity
    out, prev = [], initial
    for _, eq in sorted(by_day.items()):
        out.append(float(eq / prev - 1) if prev > 0 else 0.0)
        prev = eq
    return out


def summarize(result: BacktestResult, *, min_days_for_ratios: int = 30) -> dict[str, Any]:
    trades: list[Trade] = result.trades
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum((t.net_pnl for t in wins), Decimal(0))
    gross_loss = -sum((t.net_pnl for t in losses), Decimal(0))
    net = sum((t.net_pnl for t in trades), Decimal(0))
    gross = sum((t.gross_pnl for t in trades), Decimal(0))
    fees = sum((t.fees for t in trades), Decimal(0))
    spread = sum((t.spread_cost for t in trades), Decimal(0))
    slip = sum((t.slippage_cost for t in trades), Decimal(0))
    funding = sum((t.funding for t in trades), Decimal(0))
    dd_abs, dd_pct = max_drawdown(result.equity, result.initial_equity)
    r = [float(t.r_multiple) for t in trades]
    ci = bootstrap_mean_ci(r)
    streak = longest = 0
    for t in trades:
        streak = streak + 1 if t.net_pnl <= 0 else 0
        longest = max(longest, streak)
    in_pos = sum(1 for p in result.equity if p.in_position)
    window_bars = max(result.bars_in_window, 1)
    days = 0.0
    if result.equity:
        span = result.equity[-1].time - result.equity[0].time
        days = span.total_seconds() / 86400
    sharpe = sortino = None
    if days >= min_days_for_ratios:
        dr = _daily_returns(result.equity, result.initial_equity)
        if len(dr) > 1:
            mean = sum(dr) / len(dr)
            var = sum((x - mean) ** 2 for x in dr) / (len(dr) - 1)
            sd = math.sqrt(var)
            down = [min(x, 0.0) for x in dr]
            dsd = math.sqrt(sum(x * x for x in down) / (len(dr) - 1))
            # xyz:CL trades every day of the year
            sharpe = round(mean / sd * math.sqrt(365), 3) if sd > 0 else None
            sortino = round(mean / dsd * math.sqrt(365), 3) if dsd > 0 else None
    mean_r = sum(r) / n if n else None
    sd_r = (
        math.sqrt(sum((x - mean_r) ** 2 for x in r) / (n - 1))
        if n > 1 and mean_r is not None
        else None
    )
    t_stat = (mean_r / (sd_r / math.sqrt(n))) if (mean_r is not None and sd_r) else None
    exits: dict[str, int] = {}
    for t in trades:
        key = t.exit_reason.value if t.exit_reason else "OPEN"
        exits[key] = exits.get(key, 0) + 1
    return {
        "trades": n,
        "longs": sum(1 for t in trades if t.side.sign > 0),
        "shorts": sum(1 for t in trades if t.side.sign < 0),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": (
            round(float(gross_win / gross_loss), 3)
            if gross_loss > 0
            else (None if not wins else "inf")
        ),
        "net_pnl": _s(net),
        "gross_pnl_before_costs": _s(gross),
        "fees": _s(fees),
        "spread_cost": _s(spread),
        "slippage_cost": _s(slip),
        "funding_paid": _s(funding),
        "total_costs": _s(fees + spread + slip + funding),
        "net_return_pct": _s(net / result.initial_equity * 100, 3),
        "final_equity": _s(result.final_equity),
        "max_drawdown": _s(dd_abs),
        "max_drawdown_pct": _s(dd_pct, 3),
        "avg_win": _s(gross_win / len(wins)) if wins else None,
        "avg_loss": _s(-gross_loss / len(losses)) if losses else None,
        "expectancy": _s(net / n) if n else None,
        "expectancy_r": round(mean_r, 4) if mean_r is not None else None,
        "expectancy_r_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        "t_stat_r": round(t_stat, 3) if t_stat is not None else None,
        "longest_losing_streak": longest,
        "time_in_market_pct": round(in_pos / window_bars * 100, 2),
        "fee_drag_pct_of_gross": (
            round(float((fees + spread + slip) / abs(gross)) * 100, 1) if gross != 0 else None
        ),
        "ambiguous_bars": sum(1 for t in trades if t.ambiguous_bar),
        "exit_reasons": dict(sorted(exits.items())),
        "days": round(days, 2),
        "sharpe_daily_annualized": sharpe,
        "sortino_daily_annualized": sortino,
        "ratios_note": None
        if sharpe is not None
        else f"Sharpe/Sortino not reported: period shorter than {min_days_for_ratios} days",
    }
