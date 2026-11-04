"""Independent audit of a backtest result against the bars it was run on.

Every run is audited; any violation makes the run INVALID (it is never shown as
a result). The checks are deliberately redundant with the engine: they catch
look-ahead, impossible reference prices, broken accounting or risk breaches if
a future change to the engine introduces them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC
from decimal import Decimal

from autotrader.backtest.engine import BacktestResult, ExitReason, RiskConfig, Trade
from autotrader.strategy.base import Bar, Side

#: tolerance for Decimal division rounding in the risk budget check
_EPS = Decimal("1e-12")


def audit(
    result: BacktestResult, bars: Sequence[Bar], risk: RiskConfig, sz_decimals: int
) -> list[str]:
    v: list[str] = []
    by_open = {b.open_time: b for b in bars}
    lot = Decimal(1).scaleb(-sz_decimals)
    prev: Trade | None = None
    per_day: Counter[object] = Counter()
    for t in result.trades:
        tag = f"trade {t.id}"
        if t.exit_time is None or t.exit_price is None or t.exit_ref_price is None:
            v.append(f"{tag}: not closed")
            continue
        # ---- no look-ahead: decided at a bar close, filled strictly afterwards
        if t.entry_time < t.signal_time:
            v.append(f"{tag}: LOOK_AHEAD entry {t.entry_time} before signal {t.signal_time}")
        sig = by_open.get(t.signal_bar_open)
        if sig is None or sig.close_time != t.signal_time:
            v.append(f"{tag}: signal bar not found / inconsistent")
        if result.trade_window[0] is not None and t.signal_time < result.trade_window[0]:
            v.append(f"{tag}: signal before trade window")
        if result.trade_window[1] is not None and t.exit_time > result.trade_window[1]:
            v.append(f"{tag}: exit after trade window end")
        entry_bar = by_open.get(t.entry_time)
        if entry_bar is None:
            v.append(f"{tag}: entry not at a bar open")
        elif t.entry_ref_price != entry_bar.open:
            v.append(f"{tag}: entry reference {t.entry_ref_price} != bar open {entry_bar.open}")
        # ---- exit reference price must be one the market actually traded
        xb = by_open.get(t.exit_bar_open) if t.exit_bar_open else None
        if xb is None:
            v.append(f"{tag}: exit bar not found")
        else:
            ref = t.exit_ref_price
            if t.exit_reason is ExitReason.TAKE_PROFIT_GAP:
                ok = (xb.open >= ref) if t.side is Side.LONG else (xb.open <= ref)
            else:
                ok = xb.low <= ref <= xb.high
            if not ok:
                v.append(f"{tag}: IMPOSSIBLE exit reference {ref} outside bar {xb.low}-{xb.high}")
            if t.exit_reason is ExitReason.TAKE_PROFIT:
                through = xb.high > ref if t.side is Side.LONG else xb.low < ref
                if not through:
                    v.append(f"{tag}: target filled without trade-through")
        if t.exit_time < t.entry_time:
            v.append(f"{tag}: exit before entry")
        # ---- fills never better than the reference (costs only hurt)
        if t.side.sign * (t.entry_price - t.entry_ref_price) < 0:
            v.append(f"{tag}: entry fill better than reference")
        if t.exit_reason not in (ExitReason.TAKE_PROFIT, ExitReason.TAKE_PROFIT_GAP) and (
            t.side.sign * (t.exit_ref_price - t.exit_price) < 0
        ):
            v.append(f"{tag}: exit fill better than reference")
        # ---- stop / target geometry
        if t.side is Side.LONG and not t.stop < t.entry_price < t.target:
            v.append(f"{tag}: bracket geometry wrong for LONG")
        if t.side is Side.SHORT and not t.target < t.entry_price < t.stop:
            v.append(f"{tag}: bracket geometry wrong for SHORT")
        # ---- sizing and risk
        if t.size <= 0 or (t.size / lot) != (t.size / lot).to_integral_value():
            v.append(f"{tag}: size {t.size} not a positive multiple of {lot}")
        if t.planned_loss > t.risk_budget + _EPS:
            v.append(f"{tag}: RISK planned loss {t.planned_loss} > budget {t.risk_budget}")
        max_budget = t.equity_at_entry * risk.risk_per_trade_pct / Decimal(100)
        if t.risk_budget > max_budget + _EPS:
            v.append(f"{tag}: RISK budget above max risk per trade")
        if t.notional > risk.max_leverage * t.equity_at_entry + _EPS:
            v.append(f"{tag}: RISK leverage above {risk.max_leverage}x")
        if t.notional < risk.min_notional:
            v.append(f"{tag}: notional below minimum")
        # ---- accounting identity (exact Decimal arithmetic)
        recon = t.gross_pnl - t.spread_cost - t.slippage_cost - t.fees - t.funding
        if recon != t.net_pnl:
            v.append(f"{tag}: ACCOUNTING net {t.net_pnl} != components {recon}")
        if t.spread_cost < 0 or t.fees < 0:
            v.append(f"{tag}: negative cost component")
        if t.slippage_cost < 0:
            v.append(f"{tag}: negative slippage (fill better than modeled)")
        # ---- one position at a time
        if prev is not None and prev.exit_time is not None and t.entry_time < prev.exit_time:
            v.append(f"{tag}: OVERLAP with trade {prev.id}")
        per_day[t.entry_time.astimezone(UTC).date()] += 1
        prev = t
    for day, n in per_day.items():
        if n > risk.max_entries_per_day:
            v.append(f"RISK {n} entries on {day} > {risk.max_entries_per_day}")
    total = result.initial_equity + sum((t.net_pnl for t in result.trades), Decimal(0))
    if result.open_trade is not None:
        # live edge: the open position's entry fee and funding are already paid in cash
        total -= result.open_trade.entry_fee + result.open_trade.funding
    if total != result.final_equity:
        v.append(f"ACCOUNTING final equity {result.final_equity} != initial + net {total}")
    for p in result.equity:
        if not p.equity.is_finite() or not p.worst.is_finite():
            v.append(f"equity not finite at {p.time}")
            break
        if p.worst > p.equity:
            v.append(f"conservative mark above close mark at {p.time}")
            break
    return v
