"""Forward-test results, recomputed on every request from immutable inputs.

Inputs: the frozen config, the append-only decision journal, closed candles and
recorded funding. Nothing derived is stored, so a result can't be edited after the
fact - only recomputed. The SAME engine as the backtest simulates fills, costs,
funding and risk limits; it only sees the decisions that were recorded live.

Comparison with the backtest happens on two levels:

1. **Parity** - every stored live decision is recomputed now from the same closed
   bars. Any difference (code, data revision, look-ahead) is listed and makes the
   test INVALID.
2. **Consistency** - forward statistics vs the frozen reference backtest
   (expectancy, win rate, profit factor, trade/signal frequency, cost per trade).

Verdict: INCONCLUSIVE until the frozen thresholds (trades, days, distinct trade
days) are met; then PASS only if the after-cost expectancy CI is above zero and it
survives 2x costs, FAIL if the CI is entirely below zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autotrader.backtest.engine import BacktestResult, Engine, FundingBook, Trade
from autotrader.backtest.metrics import bootstrap_mean_ci, summarize
from autotrader.backtest.research import trade_json
from autotrader.backtest.validation import audit
from autotrader.db.models import ForwardDecision, ForwardEvent, ForwardTest
from autotrader.forward.freeze import (
    costs_from_config,
    execution_from_config,
    params_from_config,
    risk_from_config,
    strategy_code_hash,
    thresholds_from_config,
)
from autotrader.forward.recorder import STALE_AFTER, load_bars, test_state
from autotrader.ingest.funding import load_funding
from autotrader.strategy.base import Bar, Decision, Side
from autotrader.strategy.strategies import build_strategy

REPORT_SCHEMA = "forward-report/1"


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class StoredDecisions:
    """A ``Prepared`` that replays the decisions recorded live (never recomputes)."""

    def __init__(self, bars: Sequence[Bar], rows: dict[datetime, ForwardDecision]) -> None:
        self.bars = bars
        self.rows = rows

    def decide(self, i: int) -> Decision:
        r = self.rows.get(self.bars[i].open_time)
        if r is None:
            return Decision(None, "NOT_RECORDED")
        return Decision(Side(r.side) if r.side else None, r.reason, r.atr, dict(r.rationale))


def _run(
    test: ForwardTest,
    bars: Sequence[Bar],
    prepared: Any,
    funding: dict[datetime, Decimal],
    *,
    cost_scale: int = 1,
) -> tuple[BacktestResult, list[str]]:
    cfg = test.config
    params = params_from_config(cfg)
    costs = costs_from_config(cfg)
    if cost_scale != 1:
        costs = costs.scaled(fee=cost_scale, spread=cost_scale, slippage=cost_scale)
    risk = risk_from_config(cfg)
    ex = execution_from_config(cfg)
    res = Engine(
        bars,
        prepared,
        stop_atr=params.stop_atr,
        tp_r=params.tp_r,
        max_hold_bars=params.max_hold_bars,
        costs=costs,
        risk=risk,
        execution=ex,
        funding=FundingBook(funding),
        trade_start=_utc(test.starts_at),
        live_edge=True,
    ).run()
    return res, audit(res, bars, risk, ex.sz_decimals)


def _stats(trades: Sequence[Trade]) -> dict[str, Any]:
    n = len(trades)
    r = [float(t.r_multiple) for t in trades]
    wins = [t for t in trades if t.net_pnl > 0]
    gw = sum((t.net_pnl for t in wins), Decimal(0))
    gl = -sum((t.net_pnl for t in trades if t.net_pnl <= 0), Decimal(0))
    ci = bootstrap_mean_ci(r)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(float(gw / gl), 3) if gl > 0 else (None if not wins else "inf"),
        "expectancy_r": round(sum(r) / n, 4) if n else None,
        "expectancy_r_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        "avg_cost_per_trade": (
            str(
                (
                    sum(
                        (t.fees + t.spread_cost + t.slippage_cost + t.funding for t in trades),
                        Decimal(0),
                    )
                    / n
                ).quantize(Decimal("0.0001"))
            )
            if n
            else None
        ),
    }


def evaluate(session: Session, test: ForwardTest, *, now: datetime) -> dict[str, Any]:
    cfg = test.config
    starts_at = _utc(test.starts_at)
    state = test_state(session, test.id)
    rows = list(
        session.scalars(
            select(ForwardDecision)
            .where(ForwardDecision.test_id == test.id)
            .order_by(ForwardDecision.bar_open_time)
        )
    )
    by_open = {_utc(r.bar_open_time): r for r in rows}
    m30, h1 = load_bars(session, test.coin, _utc(test.history_anchor), now)
    funding = load_funding(session, test.coin, as_of=now)
    events = list(
        session.scalars(
            select(ForwardEvent).where(ForwardEvent.test_id == test.id).order_by(ForwardEvent.id)
        )
    )

    # Simulate only up to the last RECORDED bar: later closed bars haven't been decided yet.
    last_rec = _utc(rows[-1].bar_open_time) if rows else None
    sim_bars = [b for b in m30 if last_rec is not None and b.open_time <= last_rec]
    result: BacktestResult | None = None
    violations: list[str] = []
    stress: BacktestResult | None = None
    if sim_bars and any(b.open_time >= starts_at for b in sim_bars):
        live = StoredDecisions(sim_bars, by_open)
        result, violations = _run(test, sim_bars, live, funding)
        stress, _ = _run(test, sim_bars, live, funding, cost_scale=2)

    parity = _parity(test, m30, h1, rows)
    code_ok = strategy_code_hash() == test.code_hash
    summary = summarize(result) if result else None
    trades = result.trades if result else []
    observed_days = max((now - starts_at).total_seconds() / 86400, 0.0)
    th = thresholds_from_config(cfg)
    trade_days = len({t.exit_time.astimezone(UTC).date() for t in trades if t.exit_time})
    fwd = _stats(trades)
    ref = cfg.get("reference_backtest")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "paper_only": True,
        "test": {
            "id": test.id,
            "state": state,
            "strategy": cfg["strategy"],
            "params": cfg["params"],
            "params_hash": test.params_hash,
            "code_hash": test.code_hash,
            "code_hash_matches": code_ok,
            "code_version": test.code_version,
            "config_sha256": test.config_sha256,
            "created_at": _utc(test.created_at).isoformat(),
            "starts_at": starts_at.isoformat(),
            "history_anchor": _utc(test.history_anchor).isoformat(),
            "thresholds": cfg["thresholds"],
            "costs": cfg["costs"],
            "risk": cfg["risk"],
        },
        "progress": {
            "now": now.isoformat(),
            "observed_days": round(observed_days, 2),
            "decisions": len(rows),
            "signals": sum(1 for r in rows if r.side),
            "late_decisions": sum(1 for r in rows if r.late),
            "last_decision_bar": last_rec.isoformat() if last_rec else None,
            "last_closed_bar": m30[-1].close_time.isoformat() if m30 else None,
            "data_stale": bool(m30) and now - m30[-1].close_time > STALE_AFTER,
            "closed_trades": len(trades),
            "trade_days": trade_days,
        },
        "summary": summary,
        "stats": fwd,
        "open_position": _open_json(result),
        "pending_signal": (
            {"time": result.pending.signal_time.isoformat(), "side": result.pending.side.value}
            if result and result.pending
            else None
        ),
        "trades": [trade_json(t) for t in trades],
        "equity": (
            [
                [
                    p.time.isoformat(),
                    str(p.equity.quantize(Decimal("0.0001"))),
                    str(p.worst.quantize(Decimal("0.0001"))),
                ]
                for p in result.equity
            ]
            if result
            else []
        ),
        "signals": [_decision_json(r) for r in rows if r.side],
        "engine_signals": [
            {
                "time": s.signal_time.isoformat(),
                "side": s.side.value,
                "status": s.status,
                "reason": s.reason,
                "category": s.category,
                "trade_id": s.trade_id,
            }
            for s in (result.signals if result else [])
        ],
        "events": [
            {"time": _utc(e.occurred_at).isoformat(), "event": e.event, "detail": e.detail}
            for e in events
        ],
        "parity": parity,
        "comparison": _compare(ref, fwd, summary, observed_days, sum(1 for r in rows if r.side)),
        "stress_2x_costs_net": (
            str(sum((t.net_pnl for t in stress.trades), Decimal(0)).quantize(Decimal("0.0001")))
            if stress
            else None
        ),
        "audit": {"violations": violations[:50], "violation_count": len(violations)},
    }
    report["verdict"] = forward_verdict(
        n=len(trades),
        observed_days=observed_days,
        trade_days=trade_days,
        ci=fwd["expectancy_r_ci95"],
        stress_net=report["stress_2x_costs_net"],
        th=th,
        code_ok=code_ok,
        parity_ok=parity["mismatches"] == 0,
        violations=len(violations),
        state=state,
    )
    return report


def _open_json(result: BacktestResult | None) -> dict[str, Any] | None:
    if result is None or result.open_trade is None:
        return None
    t = result.open_trade
    return {
        "side": t.side.value,
        "entry_time": t.entry_time.isoformat(),
        "entry_price": str(t.entry_price),
        "size": str(t.size),
        "stop": str(t.stop),
        "target": str(t.target),
        "unrealized_pnl": str((result.open_trade_mark or Decimal(0)).quantize(Decimal("0.0001"))),
        "fees_paid": str(t.entry_fee.quantize(Decimal("0.0001"))),
        "funding_paid": str(t.funding.quantize(Decimal("0.0001"))),
    }


def _decision_json(r: ForwardDecision) -> dict[str, Any]:
    return {
        "bar_open_time": _utc(r.bar_open_time).isoformat(),
        "bar_close_time": _utc(r.bar_close_time).isoformat(),
        "evaluated_at": _utc(r.evaluated_at).isoformat(),
        "late": r.late,
        "side": r.side,
        "reason": r.reason,
        "atr": str(r.atr) if r.atr is not None else None,
        "rationale": r.rationale,
        "market": r.market,
        "strategy_version": r.strategy_version,
        "params_hash": r.params_hash,
        "input_digest": r.input_digest,
    }


def _parity(
    test: ForwardTest, m30: Sequence[Bar], h1: Sequence[Bar], rows: Sequence[ForwardDecision]
) -> dict[str, Any]:
    """Recompute each stored decision from the same closed bars (backtest code path)."""
    if not rows:
        return {"checked": 0, "mismatches": 0, "examples": []}
    prepared = build_strategy(test.strategy_id, params_from_config(test.config)).prepare(m30, h1)
    idx = {b.open_time: i for i, b in enumerate(m30)}
    mism: list[dict[str, Any]] = []
    for r in rows:
        i = idx.get(_utc(r.bar_open_time))
        if i is None:
            mism.append({"bar": _utc(r.bar_open_time).isoformat(), "issue": "BAR_MISSING_NOW"})
            continue
        d = prepared.decide(i)
        same = (
            (d.side.value if d.side else None) == r.side
            and d.reason == r.reason
            and (d.atr == r.atr or (d.atr is None and r.atr is None))
        )
        if not same:
            mism.append(
                {
                    "bar": _utc(r.bar_open_time).isoformat(),
                    "stored": [r.side, r.reason],
                    "replay": [d.side.value if d.side else None, d.reason],
                }
            )
    return {"checked": len(rows), "mismatches": len(mism), "examples": mism[:20]}


def _compare(
    ref: dict[str, Any] | None,
    fwd: dict[str, Any],
    summary: dict[str, Any] | None,
    days: float,
    n_signals: int,
) -> dict[str, Any]:
    if not ref:
        return {"available": False, "note": "no reference backtest with these parameters"}
    rs = ref["summary"]
    n = fwd["trades"]
    rows = [
        ("expectancy_r", rs.get("expectancy_r"), fwd["expectancy_r"]),
        ("win_rate", rs.get("win_rate"), fwd["win_rate"]),
        ("profit_factor", rs.get("profit_factor"), fwd["profit_factor"]),
        ("trades_per_day", ref.get("trades_per_day"), round(n / days, 4) if days >= 1 else None),
        (
            "signals_per_day",
            ref.get("signals_per_day"),
            round(n_signals / days, 4) if days >= 1 else None,
        ),
        (
            "max_drawdown_pct",
            rs.get("max_drawdown_pct"),
            summary.get("max_drawdown_pct") if summary else None,
        ),
    ]
    consistency = "INSUFFICIENT_DATA"
    ci = fwd["expectancy_r_ci95"]
    ref_er = rs.get("expectancy_r")
    if n >= 10 and ci is not None and ref_er is not None:
        if ci[0] <= ref_er <= ci[1]:
            consistency = "CONSISTENT"
        elif ref_er > ci[1]:
            consistency = "FORWARD_WORSE"
        else:
            consistency = "FORWARD_BETTER"
    return {
        "available": True,
        "reference_run_id": ref.get("run_id"),
        "reference_verdict": ref.get("verdict"),
        "reference_period": [ref.get("data_start"), ref.get("data_end")],
        "metrics": [{"metric": m, "backtest": b, "forward": f} for m, b, f in rows],
        "expectancy_consistency": consistency,
        "note": "CONSISTENT = the backtest's expectancy lies inside the forward 95% CI "
        "(requires >= 10 forward trades). Backtest numbers are in-sample for the strategy "
        "family and its holdout was exposed (ADR-029); the forward period is the first "
        "never-seen data.",
    }


def forward_verdict(
    *,
    n: int,
    observed_days: float,
    trade_days: int,
    ci: list[float] | None,
    stress_net: str | None,
    th: Any,
    code_ok: bool,
    parity_ok: bool,
    violations: int,
    state: str,
) -> dict[str, Any]:
    criteria = [
        ("code_unchanged", code_ok, "strategy/engine code identical to the frozen hash"),
        ("live_replay_parity", parity_ok, "every stored decision reproduces exactly"),
        ("zero_audit_violations", violations == 0, f"{violations} violations"),
        ("min_trades", n >= th.min_trades, f"{n} / {th.min_trades} closed trades"),
        ("min_days", observed_days >= th.min_days, f"{observed_days:.1f} / {th.min_days} days"),
        (
            "min_trade_days",
            trade_days >= th.min_trade_days,
            f"{trade_days} / {th.min_trade_days} distinct days with a closed trade",
        ),
        ("expectancy_ci_above_zero", ci is not None and ci[0] > 0, f"95% CI (R) = {ci}"),
        (
            "survives_2x_costs",
            stress_net is not None and Decimal(stress_net) > 0,
            f"net at 2x costs = {stress_net}",
        ),
    ]
    table = [{"criterion": c, "passed": ok, "detail": d} for c, ok, d in criteria]
    integrity = code_ok and parity_ok and violations == 0
    enough = n >= th.min_trades and observed_days >= th.min_days and trade_days >= th.min_trade_days
    if not integrity:
        status = "INVALID"
        reasons = [d for c, ok, d in criteria[:3] if not ok]
    elif not enough:
        status = "INCONCLUSIVE"
        reasons = ["not enough independent forward observations yet"] + [
            d for c, ok, d in criteria[3:6] if not ok
        ]
    elif all(ok for _, ok, _ in criteria):
        status, reasons = "PASS", []
    elif ci is not None and ci[1] < 0:
        status, reasons = "FAIL", ["forward after-cost expectancy is statistically negative"]
    else:
        status = "INCONCLUSIVE"
        reasons = [d for c, ok, d in criteria[6:] if not ok]
    if state != "ACTIVE" and status == "INCONCLUSIVE":
        reasons.append(f"test is {state}")
    return {"status": status, "reasons": reasons, "criteria": table}
