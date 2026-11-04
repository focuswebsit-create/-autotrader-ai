"""Research protocol, metrics, verdict and the anti-unrealistic-result audit."""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from autotrader.backtest import research
from autotrader.backtest.engine import EquityPoint, ExecConfig, RiskConfig
from autotrader.backtest.metrics import bootstrap_mean_ci, max_drawdown, summarize
from autotrader.backtest.research import (
    MarketData,
    Protocol,
    Runner,
    evaluate_strategy,
    report_digest,
    segments,
    verdict,
)
from autotrader.backtest.validation import audit
from autotrader.strategy.base import Bar, Side
from autotrader.strategy.strategies import STRATEGIES, default_params
from tests.unit.bt_helpers import T0, D, bars, flat, run

L = Side.LONG


# --------------------------------------------------------------------------- metrics


def test_max_drawdown_uses_conservative_mark() -> None:
    t = T0
    pts = [
        EquityPoint(t, D(1000), D(1000), False),
        EquityPoint(t, D(1100), D(1090), True),
        EquityPoint(t, D(1050), D(990), True),  # worst intrabar mark 990 vs hwm 1100
        EquityPoint(t, D(1200), D(1200), False),
    ]
    dd, pct = max_drawdown(pts, D(1000))
    assert dd == D(110)
    assert pct == D(110) / D(1100) * 100


def test_bootstrap_is_deterministic_and_brackets_the_mean() -> None:
    rng = random.Random(5)
    vals = [rng.gauss(0.2, 1) for _ in range(60)]
    a, b = bootstrap_mean_ci(vals), bootstrap_mean_ci(vals)
    assert a == b and a is not None
    mean = sum(vals) / len(vals)
    assert a[0] < mean < a[1]
    assert bootstrap_mean_ci([1.0]) is None


def test_summary_win_rate_profit_factor_and_costs() -> None:
    rows = [(80, 80.1, 79.9, 80)] * 2 + [(80, 84, 79.9, 84)] + [(84, 84.1, 83.9, 84)] * 2
    rows += [(84, 84.1, 83.9, 84)] * 2 + [(84, 84.1, 80, 82)] + [(82, 82.1, 81.9, 82)] * 2
    res, viol = run(bars(rows), {0: L, 5: L})
    assert viol == []
    s = summarize(res)
    assert s["trades"] == 2 and s["wins"] == 1 and s["win_rate"] == 0.5
    win, loss = (t.net_pnl for t in res.trades)
    assert s["profit_factor"] == round(float(win / -loss), 3)
    assert Decimal(s["net_pnl"]) == (win + loss).quantize(Decimal("0.0001"))
    total = sum(Decimal(s[k]) for k in ("fees", "spread_cost", "slippage_cost", "funding_paid"))
    assert Decimal(s["total_costs"]) == total
    assert s["sharpe_daily_annualized"] is None and s["ratios_note"]


def test_no_trades_summary_has_no_fake_ratios() -> None:
    res, _ = run(flat(10), {})
    s = summarize(res)
    assert s["trades"] == 0 and s["win_rate"] is None and s["profit_factor"] is None
    assert s["expectancy_r"] is None and s["expectancy_r_ci95"] is None


# --------------------------------------------------------------------------- audit


def _one_trade() -> tuple[Any, list[Bar]]:
    b = bars([(80, 80.1, 79.9, 80)] * 2 + [(80, 80.2, 78, 78.5)] + [(78.5, 78.6, 78.4, 78.5)] * 2)
    res, viol = run(b, {0: L})
    assert viol == []
    return res, b


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda t: setattr(t, "entry_time", t.signal_time - timedelta(minutes=30)), "LOOK_AHEAD"),
        (lambda t: setattr(t, "exit_ref_price", Decimal("70")), "IMPOSSIBLE"),
        (lambda t: setattr(t, "net_pnl", t.net_pnl + 1), "ACCOUNTING"),
        (lambda t: setattr(t, "entry_price", t.entry_ref_price - 1), "better than reference"),
        (lambda t: setattr(t, "planned_loss", t.risk_budget * 2), "RISK"),
        (lambda t: setattr(t, "size", Decimal("1.0005")), "multiple"),
        (lambda t: setattr(t, "size", t.size * 100), "leverage"),
    ],
)
def test_audit_detects_tampering(mutate: Any, needle: str) -> None:
    res, b = _one_trade()
    bad = copy.deepcopy(res)
    mutate(bad.trades[0])
    v = audit(bad, b, RiskConfig(), 3)
    assert any(needle in x for x in v), v


def test_audit_detects_overlap_and_final_equity_mismatch() -> None:
    res, b = _one_trade()
    bad = copy.deepcopy(res)
    dup = copy.deepcopy(bad.trades[0])
    dup.id = 2
    bad.trades.append(dup)
    v = audit(bad, b, RiskConfig(), 3)
    assert any("OVERLAP" in x for x in v)
    assert any("final equity" in x for x in v)


# --------------------------------------------------------------------------- protocol


def test_segments_are_chronological_70_15_15(recorded_market: MarketData) -> None:
    segs = segments(recorded_market, 60, Protocol().split)
    (n1, a1, b1), (n2, a2, b2), (n3, a3, b3) = segs
    assert (n1, n2, n3) == ("development", "validation", "holdout")
    assert a1 < b1 == a2 < b2 == a3 < b3
    total = (b3 - a1).total_seconds()
    assert abs((b1 - a1).total_seconds() / total - 0.70) < 0.01
    assert abs((b3 - a3).total_seconds() / total - 0.15) < 0.01
    assert b1.minute in (0, 30) and b1.second == 0


def test_selection_never_sees_validation_or_holdout(
    recorded_market: MarketData, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[datetime | None, datetime | None]] = []
    real_select = research._select

    def spy_select(runner: Runner, *a: Any, **k: Any) -> Any:
        orig = runner.run

        def logged(sid: str, p: Any, **kw: Any) -> Any:
            calls.append((kw.get("start"), kw.get("end")))
            return orig(sid, p, **kw)

        runner.run = logged  # type: ignore[method-assign]
        try:
            return real_select(runner, *a, **k)
        finally:
            runner.run = orig  # type: ignore[method-assign]

    monkeypatch.setattr(research, "_select", spy_select)
    rep = evaluate_strategy(recorded_market, "breakout_volume")
    dev_end = datetime.fromisoformat(rep["segments"][0]["end"])
    holdout_start = datetime.fromisoformat(rep["segments"][2]["start"])
    assert calls
    # the dev-only optimisation: every selection run ends at or before the dev boundary
    dev_calls = calls[: rep["optimization"]["grid_size"]]
    assert all(end is not None and end <= dev_end for _, end in dev_calls)
    # NO parameter selection anywhere (dev optimisation or walk-forward) sees the holdout
    assert all(end is not None and end <= holdout_start for _, end in calls)
    # walk-forward: train ends before test, and every test window ends before the holdout
    for fold in rep["walk_forward"]["folds"]:
        assert datetime.fromisoformat(fold["train_end"]) <= datetime.fromisoformat(
            fold["test_start"]
        )
        assert datetime.fromisoformat(fold["test_end"]) <= holdout_start
    assert rep["walk_forward"]["until"] == rep["segments"][2]["start"]
    assert holdout_start >= dev_end
    # the holdout of this dataset was already reported in PR #3 -> flagged as exposed
    it = rep["independent_test"]
    assert it["holdout_status"] == "EXPOSED" and it["status"] == "NONE"
    crit = {c["criterion"]: c["passed"] for c in rep["verdict"]["criteria"]}
    assert crit["holdout_untouched"] is False


@pytest.fixture(scope="module")
def reports(recorded_market: MarketData) -> dict[str, dict[str, Any]]:
    return {sid: evaluate_strategy(recorded_market, sid, code_version="test") for sid in STRATEGIES}


def test_full_protocol_on_real_data_is_valid_and_honest(reports: dict[str, Any]) -> None:
    for sid, rep in reports.items():
        assert rep["paper_only"] is True
        assert rep["audit"]["violation_count"] == 0, (sid, rep["audit"])
        v = rep["verdict"]
        # 104 days of 30m history can never be enough for a PASS
        assert v["status"] in ("FAIL", "INCONCLUSIVE"), sid
        crit = {c["criterion"]: c["passed"] for c in v["criteria"]}
        assert crit["history_covers_regimes"] is False
        assert rep["data"]["m30"]["bars"] == 4999 and rep["data"]["m30"]["missing_bars"] == 0
        assert rep["data"]["funding"]["records"] > 0
        mh = rep["data"]["market_hours"]
        assert mh["bars_with_trades"] == mh["bars"] and mh["bars_while_cme_closed"] > 1000
        assert sum(rep["primary"]["blocked_by_category"].values()) == sum(
            n for k, n in rep["primary"]["decision_counts"].items() if k.startswith("BLOCKED:")
        )
        s = rep["primary"]["summary"]
        assert s["trades"] == len(rep["primary"]["trades"]) > 0
        # costs are always charged and gross is never presented as net
        assert Decimal(s["fees"]) > 0
        # each figure is rounded to 4 decimals for display (exact identity is audited per trade)
        recon = Decimal(s["gross_pnl_before_costs"]) - Decimal(s["total_costs"])
        assert abs(Decimal(s["net_pnl"]) - recon) <= Decimal("0.0005")
        assert {a["provenance"] for a in rep["assumptions"]} >= {"ASSUMPTION", "VERIFIED"}
        names = [x["scenario"] for x in rep["sensitivity"]]
        assert {"costs_0.5x", "costs_2x", "slippage_10x_shock", "target_first"} <= set(names)
        assert {"fee_0.045pct_base_tier0", "fee_0.09pct_growth_off", "fee_0.10pct_previous"} <= set(
            names
        )
        assert rep["walk_forward"]["folds"]


def test_results_are_deterministic(recorded_market: MarketData, reports: dict[str, Any]) -> None:
    again = evaluate_strategy(recorded_market, "ema_rsi", code_version="test")
    assert again["report_sha256"] == reports["ema_rsi"]["report_sha256"]
    assert report_digest(again) == again["report_sha256"]


def test_trades_are_identical_when_future_data_is_removed(recorded_market: MarketData) -> None:
    """Engine-level no-look-ahead: every trade that finished before T is unchanged when the
    data after T is deleted."""
    runner = Runner(recorded_market)
    p = default_params("breakout_volume")
    start = recorded_market.m30[100].close_time
    full = runner.run("breakout_volume", p, start=start)
    cut = recorded_market.m30[3000].close_time
    short = Runner(recorded_market).run("breakout_volume", p, start=start, end=cut)
    done = [t for t in full.result.trades if t.exit_time and t.exit_time < cut]
    # last trade of the short run may be force-closed at the cut
    same = [
        t for t in short.result.trades if t.exit_reason and t.exit_reason.value != "END_OF_DATA"
    ]
    assert [research.trade_json(t) for t in done] == [research.trade_json(t) for t in same]


def test_random_walk_has_no_edge_after_costs() -> None:
    """Sanity check against unrealistic results: on a seeded random walk none of the
    strategies may show a statistically positive after-cost expectancy."""
    rng = random.Random(11)
    rows = []
    px = 80.0
    for _ in range(4000):
        o = px
        px = max(1.0, px * (1 + rng.gauss(0, 0.003)))
        hi = max(o, px) * (1 + abs(rng.gauss(0, 0.0015)))
        lo = min(o, px) * (1 - abs(rng.gauss(0, 0.0015)))
        rows.append((round(o, 3), round(hi, 3), round(lo, 3), round(px, 3), rng.randint(500, 5000)))
    start = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    m30 = bars(rows, start=start)
    # repair rounding so OHLC stays consistent
    m30 = [
        replace(b, high=max(b.high, b.open, b.close), low=min(b.low, b.open, b.close)) for b in m30
    ]
    h1 = []
    for i in range(0, len(m30) - 1, 2):
        a, b = m30[i], m30[i + 1]
        h1.append(Bar(a.open_time, b.close_time, a.open, max(a.high, b.high),
                      min(a.low, b.low), b.close, a.volume + b.volume))  # fmt: skip
    data = MarketData("xyz:CL", m30, h1, {}, "synthetic random walk", m30[-1].close_time)
    for sid in STRATEGIES:
        runner = Runner(data, execution=ExecConfig(require_funding_data=False))
        out = runner.run(sid, default_params(sid), start=m30[120].close_time)
        assert out.violations == []
        ci = out.summary["expectancy_r_ci95"]
        assert ci is None or ci[0] <= 0, (sid, out.summary)


class _Fake:
    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary


def _fake(trades: int, ci: list[float] | None, er: float, net: str) -> Any:
    return _Fake({"trades": trades, "expectancy_r_ci95": ci, "expectancy_r": er, "net_pnl": net})


def test_verdict_rules() -> None:
    proto = Protocol()
    wf_ok = {"oos": {"trades": 40, "expectancy_r": 0.3}}
    good = _fake(80, [0.1, 0.5], 0.3, "50")
    kw: dict[str, Any] = {
        "holdout": _fake(15, [0.0, 1.0], 0.2, "5"),
        "wf": wf_ok,
        "stress_2x": _fake(80, None, 0.1, "5"),
        "proto": proto,
    }
    fresh = datetime(2027, 6, 1, tzinfo=UTC)  # holdout after all exposed data
    passed = verdict(primary=good, history_days=400, violations=0, holdout_start=fresh, **kw)
    assert passed["status"] == "PASS"
    # identical evidence, but the holdout was already seen -> never PASS
    seen = verdict(primary=good, history_days=400, violations=0, holdout_start=T0, **kw)
    assert seen["status"] == "INCONCLUSIVE"
    assert any("already been seen" in r for r in seen["reasons"])
    short = verdict(primary=good, history_days=104, violations=0, **kw)
    assert short["status"] == "INCONCLUSIVE"
    assert any("history" in r for r in short["reasons"])
    few = verdict(primary=_fake(12, [-2, -0.1], -1.0, "-5"), history_days=400, violations=0, **kw)
    assert few["status"] == "INCONCLUSIVE"  # negative but too few trades to call FAIL
    bad = verdict(
        primary=_fake(40, [-0.8, -0.2], -0.5, "-40"), history_days=104, violations=0, **kw
    )
    assert bad["status"] == "FAIL"
    bad_audit = verdict(primary=good, history_days=400, violations=1, holdout_start=fresh, **kw)
    assert bad_audit["status"] == "INVALID"


def test_protocol_config_errors() -> None:
    with pytest.raises(ValueError):
        segments(MarketData("xyz:CL", flat(10), [], {}, "x", T0), 60, Protocol().split)
    with pytest.raises(ValueError):
        evaluate_strategy(MarketData("xyz:CL", flat(10), [], {}, "x", T0), "nope")


def test_exec_config_serialises() -> None:
    d = ExecConfig(session=None).as_dict()
    assert d["session_gate"] is None and d["ambiguity"] == "STOP_FIRST"
