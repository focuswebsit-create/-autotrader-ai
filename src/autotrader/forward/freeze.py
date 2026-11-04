"""Freezing a forward test: everything that decides or prices a trade is fixed up front.

A frozen config contains the strategy id/version/parameters, the cost model, risk
limits, execution settings, the verdict thresholds and a reference backtest. It is
hashed (``config_sha256``) and stored in an immutable row. The source code that
makes decisions and simulates fills is fingerprinted (``code_hash``); if that code
changes while a test is active, the recorder refuses to continue (INTEGRITY_HALT),
so a strategy can never be "improved" in the middle of its own test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autotrader.backtest.costs import CostModel, FundingMode
from autotrader.backtest.engine import Ambiguity, ExecConfig, RiskConfig
from autotrader.strategy.base import StrategyParams, params_hash
from autotrader.strategy.sessions import ReferenceSession
from autotrader.strategy.strategies import STRATEGIES, build_strategy, default_params

BAR = timedelta(minutes=30)
FORWARD_SCHEMA = "forward-test/1"

#: Files whose behaviour defines decisions and simulated fills.
_PKG = Path(__file__).resolve().parents[1]
CODE_FILES: tuple[str, ...] = (
    "strategy/indicators.py",
    "strategy/base.py",
    "strategy/strategies.py",
    "strategy/sessions.py",
    "backtest/engine.py",
    "backtest/costs.py",
)


def strategy_code_hash() -> str:
    """sha256 over the decision/fill source files (path + content, fixed order)."""
    h = hashlib.sha256()
    for rel in CODE_FILES:
        h.update(rel.encode())
        h.update(b"\0")
        h.update((_PKG / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class Thresholds:
    """When forward results may be called anything but INCONCLUSIVE (frozen per test)."""

    min_trades: int = 30
    min_days: int = 30
    #: distinct UTC days with at least one closed trade (guards against clustering)
    min_trade_days: int = 15

    def as_dict(self) -> dict[str, int]:
        return {
            "min_trades": self.min_trades,
            "min_days": self.min_days,
            "min_trade_days": self.min_trade_days,
        }


def next_bar_boundary(t: datetime) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    n = -((epoch - t) // BAR)  # ceil
    return epoch + n * BAR


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def build_config(
    strategy_id: str,
    *,
    now: datetime,
    history_anchor: datetime,
    code_version: str,
    reference: dict[str, Any] | None,
    params: StrategyParams | None = None,
    costs: CostModel | None = None,
    risk: RiskConfig | None = None,
    execution: ExecConfig | None = None,
    thresholds: Thresholds | None = None,
) -> dict[str, Any]:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy_id!r}")
    params = params or default_params(strategy_id)
    strat = build_strategy(strategy_id, params)
    costs, risk = costs or CostModel(), risk or RiskConfig()
    execution = execution or ExecConfig()
    if execution.skip_fill_rate != 0 or execution.extra_entry_delay_bars != 0:
        raise ValueError("forward tests use the baseline execution model only")
    starts_at = next_bar_boundary(now)
    if starts_at <= now:
        starts_at += BAR
    return {
        "schema": FORWARD_SCHEMA,
        "paper_only": True,
        "strategy": {"id": strategy_id, "version": strat.version, "title_he": strat.title_he},
        "params": params.as_dict(),
        "params_hash": params_hash(strategy_id, strat.version, params),
        "costs": costs.as_dict(),
        "risk": risk.as_dict(),
        "execution": execution.as_dict(),
        "thresholds": (thresholds or Thresholds()).as_dict(),
        "history_anchor": history_anchor.astimezone(UTC).isoformat(),
        "starts_at": starts_at.isoformat(),
        "code_hash": strategy_code_hash(),
        "code_version": code_version,
        "reference_backtest": reference,
    }


# --------------------------------------------------------------------------- restore


def params_from_config(cfg: dict[str, Any]) -> StrategyParams:
    sid = cfg["strategy"]["id"]
    pcls = STRATEGIES[sid][1]
    raw = cfg["params"]
    defaults = pcls()
    kwargs: dict[str, Any] = {}
    for f in fields(pcls):
        v = raw[f.name]
        d = getattr(defaults, f.name)
        if v is None:
            kwargs[f.name] = None
        elif isinstance(d, Decimal):
            kwargs[f.name] = Decimal(str(v))
        else:
            kwargs[f.name] = int(v)
    p = pcls(**kwargs)
    p.validate()
    if params_hash(sid, cfg["strategy"]["version"], p) != cfg["params_hash"]:
        raise ValueError("frozen params do not reproduce their hash")
    return p


def costs_from_config(cfg: dict[str, Any]) -> CostModel:
    c = cfg["costs"]
    return CostModel(
        taker_fee_rate=Decimal(c["taker_fee_rate"]),
        half_spread_rate=Decimal(c["half_spread_rate"]),
        slippage_rate=Decimal(c["slippage_rate"]),
        offhours_multiplier=Decimal(c["offhours_multiplier"]),
        funding_mode=FundingMode(c["funding_mode"]),
        funding_multiplier=Decimal(c["funding_multiplier"]),
        missing_funding_rate=Decimal(c["missing_funding_rate"]),
    )


def risk_from_config(cfg: dict[str, Any]) -> RiskConfig:
    r = cfg["risk"]
    return RiskConfig(
        initial_equity=Decimal(r["initial_equity"]),
        risk_per_trade_pct=Decimal(r["risk_per_trade_pct"]),
        daily_loss_limit_pct=Decimal(r["daily_loss_limit_pct"]),
        drawdown_halt_pct=Decimal(r["drawdown_halt_pct"]),
        max_leverage=Decimal(r["max_leverage"]),
        max_entries_per_day=int(r["max_entries_per_day"]),
        halt_after_consecutive_losses=int(r["halt_after_consecutive_losses"]),
        min_notional=Decimal(r["min_notional"]),
    )


def execution_from_config(cfg: dict[str, Any]) -> ExecConfig:
    e = cfg["execution"]
    gate = e["session_gate"]
    session = (
        None
        if gate is None
        else ReferenceSession(
            edge_buffer=timedelta(minutes=int(gate["edge_buffer_minutes"])),
            roll_blackout=int(gate["roll_blackout_business_days"]),
        )
    )
    return ExecConfig(
        ambiguity=Ambiguity(e["ambiguity"]),
        extra_entry_delay_bars=int(e["extra_entry_delay_bars"]),
        skip_fill_rate=Decimal(e["skip_fill_rate"]),
        seed=int(e["seed"]),
        session=session,
        max_signal_range_atr=Decimal(e["max_signal_range_atr"]),
        max_abs_funding_rate=Decimal(e["max_abs_funding_rate"]),
        require_funding_data=bool(e["require_funding_data"]),
        sz_decimals=int(e["sz_decimals"]),
    )


def thresholds_from_config(cfg: dict[str, Any]) -> Thresholds:
    t = cfg["thresholds"]
    return Thresholds(int(t["min_trades"]), int(t["min_days"]), int(t["min_trade_days"]))
