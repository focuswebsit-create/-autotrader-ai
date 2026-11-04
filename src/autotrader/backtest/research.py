"""Research protocol around the engine: chronological splits, walk-forward,
sensitivity / stress scenarios, parameter robustness and the verdict.

Protocol (fixed before looking at any result, see docs/DECISIONS.md ADR-022):

1. PRIMARY = the strategy's pre-registered default parameters (taken from the
   blueprint / strategy definition, not fitted to this data) on the whole
   history, plus the same parameters on the chronological 70/15/15 segments
   (development / validation / holdout).
2. OPTIMIZED = a small parameter grid searched on the DEVELOPMENT segment only.
   The selected parameters are locked (hash recorded) and then evaluated once on
   validation and holdout. The holdout is never used for selection.
3. WALK-FORWARD = rolling windows (train -> test). Parameters are selected on
   each train window and applied to the following unseen test window; only test
   trades are aggregated.
4. SENSITIVITY = primary re-run under cost, fill, funding and gating stresses.
5. VERDICT = PASS only if every criterion holds; FAIL only if the after-cost
   expectancy is statistically negative; otherwise INCONCLUSIVE with reasons.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from autotrader.backtest.costs import CostModel, FundingMode
from autotrader.backtest.engine import (
    Ambiguity,
    BacktestResult,
    Engine,
    ExecConfig,
    FundingBook,
    RiskConfig,
    Trade,
)
from autotrader.backtest.metrics import bootstrap_mean_ci, summarize
from autotrader.backtest.validation import audit
from autotrader.strategy.base import Bar, StrategyParams, contiguous_segments, params_hash
from autotrader.strategy.strategies import STRATEGIES, build_strategy, default_params

REPORT_SCHEMA = "backtest-report/1"
ENGINE_VERSION = "2.1.0"
BAR = timedelta(minutes=30)


@dataclass(frozen=True)
class MarketData:
    coin: str
    m30: list[Bar]
    h1: list[Bar]
    funding: dict[datetime, Decimal]
    #: free-text provenance (e.g. "PostgreSQL candles as_of=..., Hyperliquid fundingHistory")
    source: str
    as_of: datetime

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for name, bars in (("30m", self.m30), ("1h", self.h1)):
            h.update(name.encode())
            for b in bars:
                h.update(
                    f"{b.open_time.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode()
                )
        for t in sorted(self.funding):
            h.update(f"F{t.isoformat()}|{self.funding[t]}\n".encode())
        return h.hexdigest()


@dataclass(frozen=True)
class Protocol:
    split: tuple[Decimal, Decimal, Decimal] = (Decimal("0.70"), Decimal("0.15"), Decimal("0.15"))
    wf_train_days: int = 42
    wf_test_days: int = 14
    #: minimum after-cost trades for any statistical claim
    min_trades: int = 30
    min_holdout_trades: int = 10
    #: minimum history (days of signal-timeframe data) to call a result informative
    min_history_days: int = 180
    #: trades a grid candidate needs on the training data to be selectable
    min_trades_for_selection: int = 8
    #: Everything that opened before this instant has already been SEEN in reported
    #: results (PR #3 runs, 2026-11-04, incl. the 70/15/15 holdout and walk-forward
    #: folds that trained on it). A holdout that starts earlier is not independent.
    exposed_until: datetime = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)

    def as_dict(self) -> dict[str, Any]:
        return {
            "split": [str(x) for x in self.split],
            "wf_train_days": self.wf_train_days,
            "wf_test_days": self.wf_test_days,
            "min_trades": self.min_trades,
            "min_holdout_trades": self.min_holdout_trades,
            "min_history_days": self.min_history_days,
            "min_trades_for_selection": self.min_trades_for_selection,
            "exposed_until": self.exposed_until.isoformat(),
        }


@dataclass
class RunOutput:
    result: BacktestResult
    summary: dict[str, Any]
    violations: list[str]
    params: StrategyParams


@dataclass
class Runner:
    data: MarketData
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecConfig = field(default_factory=ExecConfig)
    _prep_cache: dict[tuple[str, str, Any], Any] = field(default_factory=dict)

    def run(
        self,
        strategy_id: str,
        params: StrategyParams,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        costs: CostModel | None = None,
        execution: ExecConfig | None = None,
    ) -> RunOutput:
        """One backtest. Bars before ``start`` are indicator warm-up only; nothing
        after ``end`` is visible to the strategy or the engine."""
        m30 = [b for b in self.data.m30 if end is None or b.close_time <= end]
        h1 = [b for b in self.data.h1 if end is None or b.close_time <= end]
        strategy = build_strategy(strategy_id, params)
        key = (strategy_id, params_hash(strategy_id, strategy.version, params), end)
        prep = self._prep_cache.get(key)
        if prep is None:
            prep = strategy.prepare(m30, h1)
            self._prep_cache[key] = prep
        ex = execution or self.execution
        engine = Engine(
            m30,
            prep,
            stop_atr=params.stop_atr,
            tp_r=params.tp_r,
            max_hold_bars=params.max_hold_bars,
            costs=costs or self.costs,
            risk=self.risk,
            execution=ex,
            funding=FundingBook(self.data.funding),
            trade_start=start,
            trade_end=end,
        )
        result = engine.run()
        violations = audit(result, m30, self.risk, ex.sz_decimals)
        return RunOutput(result, summarize(result), violations, params)


# --------------------------------------------------------------------------- serialization


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _d(x: Decimal | None, places: int = 6) -> str | None:
    return None if x is None else str(x.quantize(Decimal(1).scaleb(-places)))


def trade_json(t: Trade) -> dict[str, Any]:
    return {
        "id": t.id,
        "side": t.side.value,
        "signal_bar_open": _iso(t.signal_bar_open),
        "signal_time": _iso(t.signal_time),
        "entry_time": _iso(t.entry_time),
        "entry_ref_price": str(t.entry_ref_price),
        "entry_price": str(t.entry_price),
        "size": str(t.size),
        "notional": _d(t.notional, 2),
        "stop": str(t.stop),
        "target": str(t.target),
        "atr": _d(t.atr, 4),
        "risk_budget": _d(t.risk_budget, 4),
        "planned_loss": _d(t.planned_loss, 4),
        "equity_at_entry": _d(t.equity_at_entry, 4),
        "exit_time": _iso(t.exit_time),
        "exit_bar_open": _iso(t.exit_bar_open),
        "exit_ref_price": str(t.exit_ref_price) if t.exit_ref_price is not None else None,
        "exit_price": str(t.exit_price) if t.exit_price is not None else None,
        "exit_reason": t.exit_reason.value if t.exit_reason else None,
        "ambiguous_bar": t.ambiguous_bar,
        "entry_offhours": t.entry_offhours,
        "exit_offhours": t.exit_offhours,
        "crossed_data_gap": t.crossed_data_gap,
        "bars_held": t.bars_held,
        "gross_pnl": _d(t.gross_pnl, 4),
        "fees": _d(t.fees, 4),
        "spread_cost": _d(t.spread_cost, 4),
        "slippage_cost": _d(t.slippage_cost, 4),
        "funding": _d(t.funding, 4),
        "funding_hours": t.funding_hours,
        "funding_missing_hours": t.funding_missing_hours,
        "net_pnl": _d(t.net_pnl, 4),
        "r_multiple": _d(t.r_multiple, 3),
        "mae": _d(t.mae, 4),
        "mfe": _d(t.mfe, 4),
        "rationale": t.rationale,
    }


def _compact(out: RunOutput) -> dict[str, Any]:
    s = out.summary
    keys = (
        "trades", "win_rate", "profit_factor", "net_pnl", "net_return_pct", "expectancy_r",
        "expectancy_r_ci95", "max_drawdown_pct", "total_costs", "funding_paid", "days",
    )  # fmt: skip
    return {k: s.get(k) for k in keys} | {"violations": len(out.violations)}


# --------------------------------------------------------------------------- grids

GRIDS: dict[str, Callable[[], list[StrategyParams]]] = {}


def _grid(strategy_id: str, axes: dict[str, Sequence[Any]]) -> list[StrategyParams]:
    base = default_params(strategy_id)
    combos: list[dict[str, Any]] = [{}]
    for name, values in axes.items():
        combos = [c | {name: v} for c in combos for v in values]
    out: list[StrategyParams] = []
    for c in combos:
        try:
            out.append(base.replace(**c))
        except ValueError:
            continue
    return out


GRIDS["ema_rsi"] = lambda: _grid(
    "ema_rsi",
    {
        "fast": [10, 20, 30],
        "slow": [50, 100],
        "stop_atr": [Decimal("1.5"), Decimal("2.0")],
        "tp_r": [Decimal("1.5"), Decimal("2.5")],
    },
)
GRIDS["trend_pullback"] = lambda: _grid(
    "trend_pullback",
    {
        "entry_ema": [10, 20],
        "stop_atr": [Decimal("1.5"), Decimal("2.0"), Decimal("2.5")],
        "tp_r": [Decimal("1.5"), Decimal("2.0"), Decimal("3.0")],
    },
)
GRIDS["breakout_volume"] = lambda: _grid(
    "breakout_volume",
    {
        "lookback": [10, 20, 40],
        "volume_mult": [Decimal("1.0"), Decimal("1.2"), Decimal("1.5")],
        "tp_r": [Decimal("1.5"), Decimal("2.5")],
    },
)

#: parameters perturbed by +-20% for the robustness table
ROBUSTNESS: dict[str, list[str]] = {
    "ema_rsi": ["fast", "slow", "rsi_len", "stop_atr", "tp_r"],
    "trend_pullback": ["trend_fast", "trend_slow", "entry_ema", "filter_ema", "stop_atr", "tp_r"],
    "breakout_volume": ["lookback", "volume_mult", "trend_fast", "trend_slow", "stop_atr", "tp_r"],
}


# --------------------------------------------------------------------------- protocol


def _bar_floor(t: datetime) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    n = (t - epoch) // BAR
    return epoch + n * BAR


def segments(
    data: MarketData, warmup_bars: int, split: Sequence[Decimal]
) -> list[tuple[str, datetime, datetime]]:
    """Chronological development / validation / holdout boundaries (bar aligned)."""
    if len(data.m30) <= warmup_bars + 2:
        raise ValueError("not enough 30m bars for any test segment")
    start = data.m30[warmup_bars].close_time
    end = data.m30[-1].close_time
    span = end - start
    b1 = _bar_floor(start + span * float(split[0]))
    b2 = _bar_floor(start + span * float(split[0] + split[1]))
    return [("development", start, b1), ("validation", b1, b2), ("holdout", b2, end)]


def _select(
    runner: Runner, strategy_id: str, grid: list[StrategyParams], start: datetime, end: datetime,
    min_trades: int,
) -> tuple[StrategyParams, list[dict[str, Any]]]:  # fmt: skip
    """Pick the grid point with the best after-cost expectancy (R) on [start, end).
    Ties keep grid order. Falls back to the defaults if nothing qualifies."""
    best: tuple[float, int] | None = None
    chosen = default_params(strategy_id)
    table: list[dict[str, Any]] = []
    for idx, p in enumerate(grid):
        out = runner.run(strategy_id, p, start=start, end=end)
        er = out.summary["expectancy_r"]
        ok = out.summary["trades"] >= min_trades and er is not None and not out.violations
        table.append({"params": p.as_dict(), "trades": out.summary["trades"], "expectancy_r": er})
        if ok:
            score = (float(er), -idx)
            if best is None or score > best:
                best, chosen = score, p
    return chosen, table


def _trade_stats(trades: Sequence[Trade], initial: Decimal) -> dict[str, Any]:
    n = len(trades)
    net = sum((t.net_pnl for t in trades), Decimal(0))
    wins = [t for t in trades if t.net_pnl > 0]
    gw = sum((t.net_pnl for t in wins), Decimal(0))
    gl = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal(0))
    r = [float(t.r_multiple) for t in trades]
    ci = bootstrap_mean_ci(r)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(float(gw / gl), 3) if gl > 0 else (None if not wins else "inf"),
        "net_pnl": _d(net, 4),
        "net_return_pct_of_initial": _d(net / initial * 100, 3),
        "expectancy_r": round(sum(r) / n, 4) if n else None,
        "expectancy_r_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
    }


def walk_forward(
    runner: Runner,
    strategy_id: str,
    proto: Protocol,
    warmup_bars: int,
    *,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Rolling walk-forward. ``until`` (the holdout start) bounds every train AND test
    window, so no walk-forward parameter selection ever sees holdout data."""
    data = runner.data
    first = data.m30[warmup_bars].close_time
    last = until if until is not None else data.m30[-1].close_time
    train, test = timedelta(days=proto.wf_train_days), timedelta(days=proto.wf_test_days)
    grid = GRIDS[strategy_id]()
    folds: list[dict[str, Any]] = []
    oos: list[Trade] = []
    violations = 0
    t0 = first
    while t0 + train + test <= last + timedelta(seconds=1):
        tr_end = _bar_floor(t0 + train)
        te_end = _bar_floor(tr_end + test)
        chosen, _ = _select(runner, strategy_id, grid, t0, tr_end, proto.min_trades_for_selection)
        out = runner.run(strategy_id, chosen, start=tr_end, end=te_end)
        violations += len(out.violations)
        oos.extend(out.result.trades)
        folds.append(
            {
                "train_start": _iso(t0),
                "train_end": _iso(tr_end),
                "test_start": _iso(tr_end),
                "test_end": _iso(te_end),
                "params": chosen.as_dict(),
                "params_hash": params_hash(
                    strategy_id, build_strategy(strategy_id).version, chosen
                ),
                "test": _compact(out),
            }
        )
        t0 = t0 + test
    return {
        "mode": "rolling",
        "train_days": proto.wf_train_days,
        "test_days": proto.wf_test_days,
        "objective": "max after-cost expectancy (R) on the train window, "
        f">= {proto.min_trades_for_selection} trades",
        "grid_size": len(grid),
        "folds": folds,
        "oos": _trade_stats(oos, runner.risk.initial_equity),
        "violations": violations,
        "until": _iso(until),
        "note": "Walk-forward covers development + validation only and ends at the holdout "
        "start: no train window, test window or parameter selection touches the holdout."
        if until is not None
        else "Walk-forward covers the whole history (no holdout bound).",
    }


def _scenarios(base: CostModel, ex: ExecConfig) -> list[tuple[str, str, CostModel, ExecConfig]]:
    half = Decimal("0.5")
    return [
        ("baseline", "בסיס (הנחות ברירת מחדל)", base, ex),
        ("costs_0.5x", "עלויות ×0.5", base.scaled(fee=half, spread=half, slippage=half), ex),
        ("costs_2x", "עלויות ×2", base.scaled(fee=2, spread=2, slippage=2), ex),
        ("slippage_2x", "החלקה ×2", base.scaled(slippage=2), ex),
        ("slippage_10x_shock", "זעזוע החלקה ×10", base.scaled(slippage=10), ex),
        ("target_first", "נר דו־משמעי: יעד קודם (אופטימי)", base,
         replace(ex, ambiguity=Ambiguity.TARGET_FIRST)),
        ("open_proximity", "נר דו־משמעי: הקצה הקרוב לפתיחה קודם", base,
         replace(ex, ambiguity=Ambiguity.OPEN_PROXIMITY)),
        ("funding_zero", "ללא מימון (לא מציאותי)",
         replace(base, funding_mode=FundingMode.ZERO), ex),
        ("funding_adverse", "מימון תמיד נגדנו (|rate|)",
         replace(base, funding_mode=FundingMode.ADVERSE), ex),
        ("funding_2x", "מימון ×2", replace(base, funding_multiplier=Decimal(2)), ex),
        ("skip_10pct_fills", "10% מהכניסות לא מתבצעות", base,
         replace(ex, skip_fill_rate=Decimal("0.1"))),
        ("delay_1_bar", "כניסה מאוחרת בנר אחד", base, replace(ex, extra_entry_delay_bars=1)),
        ("no_session_gate", "ללא מסנן שעות CME (xyz:CL עצמו סחיר 24/7)", base,
         replace(ex, session=None)),
        ("fee_0.0045pct_maker_tier0", "עמלה 0.0045% (כמו Maker בדרגה 0, אופטימי)",
         replace(base, taker_fee_rate=Decimal("0.000045")), ex),
        ("fee_0.045pct_base_tier0", "עמלה 0.045% (Taker בסיסי דרגה 0, ללא HIP-3)",
         replace(base, taker_fee_rate=Decimal("0.00045")), ex),
        ("fee_0.09pct_growth_off", "עמלה 0.09% (אם Growth Mode יבוטל, הסקה)",
         replace(base, taker_fee_rate=Decimal("0.0009")), ex),
        ("fee_0.10pct_previous", "עמלה 0.10% (ההנחה הקודמת)",
         replace(base, taker_fee_rate=Decimal("0.001")), ex),
    ]  # fmt: skip


def _perturb(p: StrategyParams, name: str, factor: Decimal) -> StrategyParams | None:
    v = getattr(p, name)
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        nv: Any = max(2, round(v * factor))
        if nv == v:
            nv = v + (1 if factor > 1 else -1)
    elif isinstance(v, Decimal):
        nv = (v * factor).quantize(Decimal("0.01"))
    else:
        return None
    try:
        return p.replace(**{name: nv})
    except ValueError:
        return None


def verdict(
    *,
    primary: RunOutput,
    holdout: RunOutput,
    wf: dict[str, Any],
    stress_2x: RunOutput,
    history_days: float,
    proto: Protocol,
    violations: int,
    holdout_start: datetime | None = None,
) -> dict[str, Any]:
    s = primary.summary
    ci = s["expectancy_r_ci95"]
    n = s["trades"]
    criteria = [
        ("zero_audit_violations", violations == 0, f"{violations} violations"),
        ("min_trades", n >= proto.min_trades, f"{n} trades (need >= {proto.min_trades})"),
        (
            "expectancy_ci_above_zero",
            ci is not None and ci[0] > 0,
            f"95% CI of expectancy (R) = {ci}",
        ),
        (
            "holdout_profitable",
            holdout.summary["trades"] >= proto.min_holdout_trades
            and Decimal(holdout.summary["net_pnl"]) > 0,
            f"holdout {holdout.summary['trades']} trades, net {holdout.summary['net_pnl']} "
            f"(need >= {proto.min_holdout_trades} trades and net > 0)",
        ),
        (
            "walk_forward_oos_positive",
            wf["oos"]["trades"] >= proto.min_trades_for_selection
            and (wf["oos"]["expectancy_r"] or 0) > 0,
            f"walk-forward OOS {wf['oos']['trades']} trades, "
            f"expectancy {wf['oos']['expectancy_r']} R",
        ),
        (
            "survives_2x_costs",
            Decimal(stress_2x.summary["net_pnl"]) > 0,
            f"net at 2x costs = {stress_2x.summary['net_pnl']}",
        ),
        (
            "history_covers_regimes",
            history_days >= proto.min_history_days,
            f"{history_days:.0f} days of 30m history (need >= {proto.min_history_days})",
        ),
        (
            "holdout_untouched",
            holdout_start is not None and holdout_start >= proto.exposed_until,
            f"holdout starts {_iso(holdout_start)}; data before "
            f"{_iso(proto.exposed_until)} has already been seen in reported results",
        ),
    ]
    table = [{"criterion": c, "passed": ok, "detail": d} for c, ok, d in criteria]
    if violations:
        status = "INVALID"
        reasons = ["audit violations - result must not be used"]
    elif all(ok for _, ok, _ in criteria):
        status, reasons = "PASS", []
    elif n >= proto.min_trades and ci is not None and ci[1] < 0:
        status = "FAIL"
        reasons = ["after-cost expectancy is statistically negative (95% CI entirely below 0)"]
    else:
        status = "INCONCLUSIVE"
        reasons = [d for c, ok, d in criteria if not ok]
        if n and s["expectancy_r"] is not None and s["expectancy_r"] <= 0:
            reasons.insert(0, "no evidence of an edge: point estimate of expectancy <= 0 R")
    return {"status": status, "reasons": reasons, "criteria": table}


def evaluate_strategy(
    data: MarketData,
    strategy_id: str,
    *,
    costs: CostModel | None = None,
    risk: RiskConfig | None = None,
    execution: ExecConfig | None = None,
    proto: Protocol | None = None,
    code_version: str = "unknown",
    params: StrategyParams | None = None,
) -> dict[str, Any]:
    """Run the full research protocol for one strategy and return a JSON report."""
    if strategy_id not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy_id!r}")
    proto = proto or Protocol()
    runner = Runner(data, costs or CostModel(), risk or RiskConfig(), execution or ExecConfig())
    params = params or default_params(strategy_id)
    strat = build_strategy(strategy_id, params)
    warm = strat.warmup_bars()
    segs = segments(data, warm, proto.split)
    start, end = segs[0][1], segs[-1][2]

    primary = runner.run(strategy_id, params, start=start, end=end)
    seg_out = {name: runner.run(strategy_id, params, start=a, end=b) for name, a, b in segs}

    # optimization on development only, locked before validation/holdout
    grid = GRIDS[strategy_id]()
    dev = segs[0]
    chosen, grid_table = _select(
        runner, strategy_id, grid, dev[1], dev[2], proto.min_trades_for_selection
    )
    chosen_hash = params_hash(strategy_id, strat.version, chosen)
    opt_out = {name: runner.run(strategy_id, chosen, start=a, end=b) for name, a, b in segs}

    holdout_start = segs[2][1]
    wf = walk_forward(runner, strategy_id, proto, warm, until=holdout_start)

    sens: list[dict[str, Any]] = []
    stress_2x = primary
    for key, label, c, ex in _scenarios(runner.costs, runner.execution):
        out = runner.run(strategy_id, params, start=start, end=end, costs=c, execution=ex)
        if key == "costs_2x":
            stress_2x = out
        sens.append({"scenario": key, "label_he": label} | _compact(out))

    robust: list[dict[str, Any]] = []
    for name in ROBUSTNESS[strategy_id]:
        for factor in (Decimal("0.8"), Decimal("1.2")):
            p2 = _perturb(params, name, factor)
            if p2 is None:
                continue
            out = runner.run(strategy_id, p2, start=start, end=end)
            robust.append({"param": name, "value": str(getattr(p2, name))} | _compact(out))

    all_violations = (
        primary.violations
        + [v for o in seg_out.values() for v in o.violations]
        + [v for o in opt_out.values() for v in o.violations]
    )
    total_violations = len(all_violations) + wf["violations"]
    hist_days = _history_days(data.m30)
    v = verdict(
        primary=primary,
        holdout=seg_out["holdout"],
        wf=wf,
        stress_2x=stress_2x,
        history_days=hist_days,
        proto=proto,
        violations=total_violations,
        holdout_start=holdout_start,
    )
    res = primary.result
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "paper_only": True,
        "strategy": {
            "id": strategy_id,
            "version": strat.version,
            "title_he": strat.title_he,
            "description_he": strat.description_he,
        },
        "params": params.as_dict(),
        "params_hash": params_hash(strategy_id, strat.version, params),
        "data": _data_info(data, hist_days),
        "config": {
            "costs": runner.costs.as_dict(),
            "risk": runner.risk.as_dict(),
            "execution": runner.execution.as_dict(),
            "protocol": proto.as_dict(),
        },
        "assumptions": runner.costs.assumptions() + _other_assumptions(),
        "trade_window": {"start": _iso(start), "end": _iso(end)},
        "primary": {
            "summary": primary.summary,
            "trades": [trade_json(t) for t in res.trades],
            "equity": [[_iso(p.time), _d(p.equity, 4), _d(p.worst, 4)] for p in res.equity],
            "decision_counts": res.decision_counts,
            "blocked_by_category": _by_category(res.signals),
            "signals": [
                {
                    "time": _iso(s.signal_time),
                    "side": s.side.value,
                    "status": s.status,
                    "reason": s.reason,
                    "category": s.category,
                    "trade_id": s.trade_id,
                }
                for s in res.signals
            ],
            "events": res.events,
            "halted": res.halted,
        },
        "segments": [
            {"name": name, "start": _iso(a), "end": _iso(b)} | _compact(seg_out[name])
            for name, a, b in segs
        ],
        "optimization": {
            "objective": "max after-cost expectancy (R) on DEVELOPMENT only, "
            f">= {proto.min_trades_for_selection} trades",
            "grid_size": len(grid),
            "grid": grid_table,
            "selected_params": chosen.as_dict(),
            "selected_params_hash": chosen_hash,
            "segments": [
                {"name": name, "start": _iso(a), "end": _iso(b)} | _compact(opt_out[name])
                for name, a, b in segs
            ],
            "note": "Development results of the selected parameters are in-sample and "
            "optimistic by construction. Only validation/holdout are out-of-sample.",
        },
        "independent_test": _independent_test(data, proto, holdout_start),
        "walk_forward": wf,
        "sensitivity": sens,
        "robustness": robust,
        "verdict": v,
        "audit": {"violations": all_violations[:50], "violation_count": total_violations},
        "code": {"commit": code_version, "engine_version": ENGINE_VERSION},
    }
    report["report_sha256"] = report_digest(report)
    return report


def _by_category(signals: Sequence[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for sig in signals:
        if sig.status == "BLOCKED":
            out[sig.category] = out.get(sig.category, 0) + 1
    return dict(sorted(out.items()))


def _independent_test(data: MarketData, proto: Protocol, holdout_start: datetime) -> dict[str, Any]:
    fresh = [b for b in data.m30 if b.open_time >= proto.exposed_until]
    untouched = holdout_start >= proto.exposed_until
    return {
        "holdout_untouched": untouched,
        "holdout_status": "UNTOUCHED" if untouched else "EXPOSED",
        "exposed_until": _iso(proto.exposed_until),
        "fresh_bars_after_exposure": len(fresh),
        "fresh_days_after_exposure": round(len(fresh) / 48, 2),
        "status": "AVAILABLE" if untouched else "NONE",
        "note": "The holdout segment was used for parameter selection inside walk-forward "
        "folds in the PR #3 runs and its results were reported. There is currently NO "
        "untouched independent test; only data after exposed_until (forward data) can be one."
        if not untouched
        else "Holdout starts after all previously reported data.",
    }


def report_digest(report: dict[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k not in ("report_sha256", "generated_at")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _history_days(bars: Sequence[Bar]) -> float:
    if not bars:
        return 0.0
    return (bars[-1].close_time - bars[0].open_time).total_seconds() / 86400


def _data_info(data: MarketData, hist_days: float) -> dict[str, Any]:
    def info(bars: Sequence[Bar], step: timedelta) -> dict[str, Any]:
        segs = contiguous_segments(bars)
        missing = 0
        for (_, e1), (s2, _) in itertools.pairwise(segs):
            missing += int((bars[s2].open_time - bars[e1 - 1].close_time) / step)
        return {
            "bars": len(bars),
            "first_open": _iso(bars[0].open_time) if bars else None,
            "last_close": _iso(bars[-1].close_time) if bars else None,
            "gaps": len(segs) - 1 if segs else 0,
            "missing_bars": missing,
        }

    ft = sorted(data.funding)
    return {
        "coin": data.coin,
        "source": data.source,
        "as_of": _iso(data.as_of),
        "m30": info(data.m30, timedelta(minutes=30)),
        "h1": info(data.h1, timedelta(hours=1)),
        "funding": {
            "records": len(ft),
            "first": _iso(ft[0]) if ft else None,
            "last": _iso(ft[-1]) if ft else None,
            "provenance": "VERIFIED (Hyperliquid fundingHistory)" if ft else "MISSING",
        },
        "history_days_30m": round(hist_days, 2),
        "market_hours": _market_hours(data.m30),
        "fingerprint_sha256": data.fingerprint(),
        "limitation": "Hyperliquid candleSnapshot returns only the most recent 5,000 bars per "
        "interval (~104 days at 30m). No longer history was used or fabricated.",
    }


def _market_hours(bars: Sequence[Bar]) -> dict[str, Any]:
    """Observed tradability of xyz:CL itself, separate from the CME strategy filter."""
    from statistics import median

    from autotrader.strategy.sessions import ReferenceSession

    closed = [b for b in bars if not ReferenceSession.reference_trading(b.open_time)]
    open_ = [b for b in bars if ReferenceSession.reference_trading(b.open_time)]
    with_trades = sum(1 for b in bars if b.volume > 0 and (b.trades is None or b.trades > 0))

    def med(xs: Sequence[Bar]) -> str | None:
        return str(median(b.volume for b in xs).quantize(Decimal(1))) if xs else None

    return {
        "bars": len(bars),
        "bars_with_trades": with_trades,
        "min_trades_per_bar": min((b.trades for b in bars if b.trades is not None), default=None),
        "bars_while_cme_closed": len(closed),
        "median_volume_cme_open": med(open_),
        "median_volume_cme_closed": med(closed),
        "conclusion": "xyz:CL is a 24/7 market: every bar in the data has trades, including "
        "weekends and CME holidays. The CME calendar is a STRATEGY FILTER for new entries "
        "(thin, reference-less price discovery), not a tradability constraint; exits are "
        "always simulated when the perp trades.",
    }


def _other_assumptions() -> list[dict[str, str]]:
    from autotrader.strategy.sessions import PROVENANCE

    return [
        {
            "item": "cme_strategy_filter",
            "value": "CME Globex CL: Sun 18:00-Fri 17:00 NY, daily break 17-18",
            "provenance": "UNVERIFIED",
            "note": PROVENANCE + ". STRATEGY FILTER only: xyz:CL itself trades 24/7 (verified "
            "from candles). New entries are filtered outside CME hours, 30 min around edges, "
            "on holidays and in the 2 business days up to each CL expiry; exits are never "
            "blocked.",
        },
        {
            "item": "stop_fill",
            "value": "stop-market",
            "provenance": "ASSUMPTION",
            "note": "Stop fills at the stop level (or the open if the bar gapped through) "
            "minus spread and slippage. Fill prices include modelled costs, so they can lie "
            "slightly outside the bar's printed range.",
        },
        {
            "item": "ambiguous_bar",
            "value": "STOP first",
            "provenance": "ASSUMPTION",
            "note": "When a bar touches both stop and target there is no intrabar evidence, "
            "so the stop is assumed to trade first. See sensitivity for alternatives.",
        },
        {
            "item": "min_notional",
            "value": "10 USDC",
            "provenance": "ASSUMPTION",
            "note": "Hyperliquid minimum order value from prior knowledge, not re-verified.",
        },
        {
            "item": "risk_rules_in_backtest",
            "value": "cooldown",
            "provenance": "ASSUMPTION",
            "note": "3 consecutive losses pause entries until 00:00 UTC (the paper engine "
            "will require manual review); a 5% drawdown halts entries for the rest of the run.",
        },
    ]


def finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)
