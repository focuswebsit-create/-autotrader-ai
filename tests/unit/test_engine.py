"""Engine mechanics on synthetic bars: fills, costs, ambiguity, gaps, funding, risk gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from autotrader.backtest.costs import CostModel, FundingMode, floor_size, round_price
from autotrader.backtest.engine import Ambiguity, ExecConfig, ExitReason, RiskConfig
from autotrader.strategy.base import Side
from tests.unit.bt_helpers import NO_COSTS, STEP, T0, D, Scripted, bars, flat, run

L, S = Side.LONG, Side.SHORT
BASE = CostModel(missing_funding_rate=D(0))


def _long_stop_bars() -> list:
    return bars(
        [
            (80, 80.1, 79.9, 80),  # 0 signal bar
            (80, 80.5, 79.5, 80),  # 1 entry at open
            (80, 80.2, 78.0, 78.5),  # 2 stop 78.548 hit
            *[(78.5, 78.6, 78.4, 78.5)] * 3,
        ]
    )


def test_entry_is_next_bar_open_with_adverse_costs_and_exact_accounting() -> None:
    res, viol = run(_long_stop_bars(), {0: L}, costs=BASE)
    assert viol == []
    (t,) = res.trades
    assert t.signal_time == T0 + STEP and t.entry_time == T0 + STEP
    assert t.entry_ref_price == D(80)
    assert t.entry_price == D("80.048")  # 80 * (1 + 0.0001 + 0.0005), rounded up
    assert t.stop == D("78.548") and t.target == D("83.048")
    # sizing: risk budget 0.25% of 1000 = 2.5 incl. conservative stop costs and fees
    est = t.stop * (1 - D("0.0006") * 2)
    per_unit = (t.entry_price - est) + D("0.001") * (t.entry_price + est)
    assert t.size == floor_size(D("2.5") / per_unit, 3) == D("1.426")
    assert t.planned_loss <= t.risk_budget == D("2.5")
    assert t.exit_reason is ExitReason.STOP
    assert t.exit_price == D("78.500")  # 78.548 * (1 - 0.0006) rounded down
    fees = D("0.001") * (t.entry_price + t.exit_price) * t.size
    assert t.fees == fees
    assert t.net_pnl == (t.exit_price - t.entry_price) * t.size - fees
    assert res.final_equity == D(1000) + t.net_pnl
    assert t.gross_pnl - t.spread_cost - t.slippage_cost - t.fees - t.funding == t.net_pnl
    assert t.r_multiple < D(-0.9)


def test_never_fills_at_signal_close_even_if_strategy_signals_last_bars() -> None:
    b = flat(4)
    res, viol = run(b, {2: L, 3: L}, costs=BASE)
    # bar 3 is the last (reserved for END_OF_DATA); a signal at bar 2 has no bar to
    # trade after the next one -> both blocked
    assert res.trades == [] and viol == []
    assert {s.reason for s in res.signals} == {"NO_NEXT_BAR"}


def test_ambiguous_bar_stop_first_by_default_and_sensitivity_alternatives() -> None:
    b = bars(
        [
            (80, 80.1, 79.9, 80),
            (80, 80.1, 79.9, 80),
            (80, 84.0, 78.0, 80),  # touches stop 78.548 AND target 83.048
            *[(80, 80.1, 79.9, 80)] * 2,
        ]
    )
    sl, v1 = run(b, {0: L}, costs=BASE)
    tp, v2 = run(b, {0: L}, costs=BASE, ex=ExecConfig(ambiguity=Ambiguity.TARGET_FIRST))
    assert v1 == v2 == []
    assert sl.trades[0].exit_reason is ExitReason.STOP and sl.trades[0].ambiguous_bar
    assert tp.trades[0].exit_reason is ExitReason.TAKE_PROFIT and tp.trades[0].ambiguous_bar
    assert sl.trades[0].net_pnl < 0 < tp.trades[0].net_pnl


def test_open_proximity_policy() -> None:
    # open 80 -> high 83.1 is 3.1 away, low 78.5 is 1.5 away -> low (stop) first
    b = bars([(80, 80.1, 79.9, 80)] * 2 + [(80, 83.1, 78.5, 80)] + [(80, 80.1, 79.9, 80)] * 2)
    res, _ = run(b, {0: L}, costs=BASE, ex=ExecConfig(ambiguity=Ambiguity.OPEN_PROXIMITY))
    assert res.trades[0].exit_reason is ExitReason.STOP


def test_target_needs_trade_through_not_touch() -> None:
    b = bars(
        [(80, 80.1, 79.9, 80)] * 2
        + [(80, 83.048, 79.9, 83.0)]  # exactly touches 83.048: not filled
        + [(83, 83.1, 82.9, 83)]  # trades through
        + [(83, 83.1, 82.9, 83)]
    )
    res, viol = run(b, {0: L}, costs=BASE)
    assert viol == []
    t = res.trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_bar_open == b[3].open_time and t.exit_price == t.target == D("83.048")


def test_gap_through_stop_fills_at_open_and_can_exceed_planned_loss() -> None:
    b = bars([(80, 80.1, 79.9, 80)] * 2 + [(77, 77.2, 76.8, 77)] + [(77, 77.1, 76.9, 77)] * 2)
    res, viol = run(b, {0: L}, costs=BASE)
    assert viol == []
    t = res.trades[0]
    assert t.exit_reason is ExitReason.STOP_GAP
    assert t.exit_ref_price == D(77)
    assert t.exit_price == round_price(D(77) * (1 - D("0.0006")), 3, "down")
    assert -t.net_pnl > t.planned_loss  # gap risk is real and is reported, not hidden


def test_gap_beyond_target_is_filled_at_target_not_better() -> None:
    b = bars([(80, 80.1, 79.9, 80)] * 2 + [(85, 85.5, 84.5, 85)] + [(85, 85.1, 84.9, 85)] * 2)
    res, viol = run(b, {0: L}, costs=BASE)
    assert viol == []
    t = res.trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT_GAP and t.exit_price == t.target


def test_short_mirror() -> None:
    b = bars(
        [(80, 80.1, 79.9, 80)] * 2
        + [(80, 80.2, 76.0, 76.5)]  # short target 80*(1-.0006)=79.952 - 3 = 76.952
        + [(76.5, 76.6, 76.4, 76.5)] * 2
    )
    res, viol = run(b, {0: S}, costs=BASE)
    assert viol == []
    t = res.trades[0]
    assert t.side is S and t.entry_price == D("79.952")
    assert t.stop == D("81.452") and t.target == D("76.952")
    assert t.exit_reason is ExitReason.TAKE_PROFIT and t.net_pnl > 0


def test_funding_sign_hours_and_missing_assumption() -> None:
    # entry at 14:30, exit at END_OF_DATA 17:00 -> funding hours 15:00, 16:00, 17:00
    b = flat(7)
    rates = {T0 + timedelta(hours=1): D("0.0001"), T0 + timedelta(hours=2): D("0.0001")}
    costs = CostModel(missing_funding_rate=D("0.00001"))
    lres, _ = run(b, {0: L}, costs=costs, funding=rates)
    sres, _ = run(b, {0: S}, costs=costs, funding=rates)
    lt, st = lres.trades[0], sres.trades[0]
    assert lt.funding_hours == st.funding_hours == 3
    assert lt.funding_missing_hours == st.funding_missing_hours == 1
    # positive rate: the long pays, the short receives; the missing hour costs both
    assert lt.funding == lt.size * D(80) * (D("0.0002") + D("0.00001"))
    assert st.funding == st.size * D(80) * (D("-0.0002") + D("0.00001"))
    zero, _ = run(b, {0: L}, costs=replace(costs, funding_mode=FundingMode.ZERO), funding=rates)
    assert zero.trades[0].funding == 0
    adverse, _ = run(
        b, {0: S}, costs=replace(costs, funding_mode=FundingMode.ADVERSE), funding=rates
    )
    assert adverse.trades[0].funding > 0


def test_entry_exactly_on_the_hour_does_not_pay_that_hour() -> None:
    b = flat(4, start=T0 - STEP)  # signal bar 13:30-14:00, entry at 14:00
    res, _ = run(b, {0: L}, costs=CostModel(missing_funding_rate=D("0.001")))
    t = res.trades[0]
    assert t.entry_time == T0
    assert t.funding_hours == 1  # 15:00 only (exit at END_OF_DATA 15:30)


def test_session_gate_blocks_weekend_and_daily_break() -> None:
    sat = T0 + timedelta(days=4)  # Saturday
    res, _ = run(flat(5, start=sat), {0: L}, costs=BASE)
    assert res.trades == [] and res.signals[0].reason == "REFERENCE_SESSION_CLOSED"
    # disabled gate (sensitivity) trades and marks the fill as off-hours
    res2, viol = run(flat(5, start=sat), {0: L}, costs=BASE, ex=ExecConfig(session=None))
    assert viol == [] and res2.trades[0].entry_offhours
    assert res2.trades[0].entry_price == round_price(D(80) * (1 + D("0.0006") * 2), 3, "up")


def test_data_gap_and_dead_bar_block_entries() -> None:
    b = flat(6)
    gapped = b[:1] + b[2:]
    res, _ = run(gapped, {0: L}, costs=BASE)
    assert res.trades == [] and res.signals[0].reason == "DATA_GAP"
    dead = list(b)
    dead[1] = replace(dead[1], volume=D(0))
    res2, _ = run(dead, {0: L}, costs=BASE)
    assert res2.trades == [] and res2.signals[0].reason == "NO_PLAUSIBLE_EXECUTION"


def test_abnormal_volatility_and_funding_gates() -> None:
    b = bars([(80, 86, 79, 80)] + [(80, 80.1, 79.9, 80)] * 4)  # range 7 > 4 x ATR(1)
    res, _ = run(b, {0: L}, costs=BASE)
    assert res.signals[0].reason == "ABNORMAL_VOLATILITY"
    res2, _ = run(flat(5), {0: L}, costs=BASE, funding={T0: D("0.002")})
    assert res2.signals[0].reason == "ABNORMAL_FUNDING"


def test_leverage_cap_and_min_notional() -> None:
    # a tiny ATR would size a huge position: capped at 2x equity
    res, viol = run(flat(6), {0: L}, costs=NO_COSTS, atr=D("0.05"), stop_atr=D("0.2"))
    assert viol == []
    t = res.trades[0]
    assert t.notional <= 2 * D(1000) and t.notional > D(1900)
    # a very wide stop makes the position smaller than the 10 USDC minimum
    res2, _ = run(flat(6), {0: L}, costs=BASE, atr=D(40), stop_atr=D(10))
    assert res2.trades == [] and res2.signals[0].reason == "INVALID_STOP"
    res3, _ = run(flat(6), {0: L}, costs=BASE, atr=D(10), stop_atr=D(7))
    assert res3.trades == [] and res3.signals[0].reason == "SIZE_BELOW_MINIMUM"


def _losing_series(n_trades: int) -> tuple[list, dict[int, Side]]:
    rows: list[tuple] = []
    sig: dict[int, Side] = {}
    for _ in range(n_trades):
        sig[len(rows)] = L
        rows += [(80, 80.1, 79.9, 80), (80, 80.1, 79.9, 80), (80, 80.1, 78.0, 80)]
    rows += [(80, 80.1, 79.9, 80)] * 3
    return bars(rows), sig


def test_consecutive_losses_pause_entries_until_next_utc_day() -> None:
    b, sig = _losing_series(5)
    res, viol = run(b, sig, costs=BASE, risk=RiskConfig(daily_loss_limit_pct=D(20)))
    assert viol == []
    assert len(res.trades) == 3
    assert any(s.reason == "CONSECUTIVE_LOSSES_COOLDOWN" for s in res.signals)
    assert any(e["event"] == "CONSECUTIVE_LOSSES" for e in res.events)


def test_daily_loss_limit_blocks_new_entries() -> None:
    b, sig = _losing_series(4)
    risk = RiskConfig(daily_loss_limit_pct=D("0.4"), halt_after_consecutive_losses=10)
    res, viol = run(b, sig, costs=BASE, risk=risk)
    assert viol == [] and len(res.trades) == 2
    assert any(s.reason == "DAILY_LOSS_LIMIT" for s in res.signals)


def test_max_entries_per_day() -> None:
    rows: list[tuple] = []
    sig: dict[int, Side] = {}
    for _ in range(6):  # winners: target hit on the bar after entry
        sig[len(rows)] = L
        rows += [(80, 80.1, 79.9, 80), (80, 80.1, 79.9, 80), (80, 84, 79.9, 84)]
    rows += [(84, 84.1, 83.9, 84)] * 2
    # start 08:00 UTC so all six setups fall inside one session day
    res, viol = run(bars(rows, start=T0 - timedelta(hours=6)), sig, costs=BASE)
    assert viol == []
    assert len(res.trades) == 4
    assert any(s.reason == "MAX_ENTRIES_PER_DAY" for s in res.signals)


def test_drawdown_halt_is_permanent_for_the_run() -> None:
    b, sig = _losing_series(8)
    risk = RiskConfig(
        risk_per_trade_pct=D(2),
        drawdown_halt_pct=D(5),
        daily_loss_limit_pct=D(20),
        halt_after_consecutive_losses=50,
        max_entries_per_day=50,
    )
    res, viol = run(b, sig, costs=BASE, risk=risk)
    assert viol == []
    assert res.halted == "DRAWDOWN_HALT"
    assert len(res.trades) < 8
    assert res.signals[-1].reason == "DRAWDOWN_HALT"


def test_one_position_at_a_time_strategy_not_asked_while_in_position() -> None:
    b = flat(10)
    prep = Scripted({0: L, 2: L, 4: S})
    res, viol = run(b, prep, costs=BASE)
    assert viol == [] and len(res.trades) == 1
    assert res.trades[0].exit_reason is ExitReason.END_OF_DATA
    assert prep.calls == [0]  # never evaluated while a position was open


def test_time_exit_at_next_bar_open() -> None:
    res, viol = run(flat(10), {0: L}, costs=BASE, max_hold=3)
    assert viol == []
    t = res.trades[0]
    assert t.exit_reason is ExitReason.TIME and t.bars_held == 3
    assert t.exit_time == T0 + 4 * STEP


def test_skipped_fills_are_seeded_and_deterministic() -> None:
    b, sig = _losing_series(6)
    ex = ExecConfig(skip_fill_rate=D("0.5"), seed=1)
    r1, _ = run(b, sig, costs=BASE, ex=ex)
    r2, _ = run(b, sig, costs=BASE, ex=ex)
    assert [s.reason for s in r1.signals] == [s.reason for s in r2.signals]
    assert any(s.reason == "SKIPPED_FILL_SIMULATED" for s in r1.signals)


def test_trade_window_limits_signals_and_forces_exit_at_end() -> None:
    b = flat(12)
    res, viol = run(b[:8], {1: L, 4: L}, costs=BASE, start=b[3].close_time, end=b[7].close_time)
    assert viol == []
    assert len(res.trades) == 1 and res.trades[0].signal_time == b[4].close_time
    assert res.trades[0].exit_reason is ExitReason.END_OF_DATA


@pytest.mark.parametrize(
    ("px", "mode", "want"),
    [
        (D("96.40351"), "up", D("96.404")),
        (D("96.40351"), "down", D("96.403")),
        (D("123.456"), "nearest", D("123.46")),  # 5 significant figures
        (D("123.451"), "down", D("123.45")),
    ],
)
def test_price_rounding(px: Decimal, mode: str, want: Decimal) -> None:
    assert round_price(px, 3, mode) == want


def test_invalid_configs_rejected() -> None:
    with pytest.raises(ValueError):
        RiskConfig(risk_per_trade_pct=D(5)).validate()
    with pytest.raises(ValueError):
        CostModel(taker_fee_rate=D("-0.1")).validate()
    with pytest.raises(ValueError):
        RiskConfig(max_leverage=D(50)).validate()
