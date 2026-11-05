"""Frozen configuration of a PAPER trading account (risk, execution, strategy).

The configuration is written once into ``paper_accounts.config`` with its sha256.
Changing any value means creating a new account (a new ledger) - results of an
account are never produced by a mix of settings. Defaults are the blueprint's
conservative limits and the fee observed on xyz:CL fills (ADR-028).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from autotrader.backtest.costs import OBSERVED_TAKER_FEE

_PKG = Path(__file__).resolve().parents[1]

#: Source files that decide and price PAPER trades. If they change after an
#: account was created, the account refuses new entries (STRATEGY_CODE_CHANGED).
PAPER_CODE_FILES: tuple[str, ...] = (
    "strategy/indicators.py",
    "strategy/base.py",
    "strategy/strategies.py",
    "strategy/sessions.py",
    "backtest/costs.py",
    "market/models.py",
    "paper/config.py",
    "paper/book.py",
    "paper/engine.py",
)


def paper_code_hash() -> str:
    h = hashlib.sha256()
    for rel in PAPER_CODE_FILES:
        h.update(rel.encode() + b"\0" + (_PKG / rel).read_bytes() + b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class PaperRisk:
    initial_equity: Decimal = Decimal(1000)
    risk_per_trade_pct: Decimal = Decimal("0.25")
    daily_loss_limit_pct: Decimal = Decimal("1.0")
    drawdown_halt_pct: Decimal = Decimal("5.0")
    max_leverage: Decimal = Decimal(2)
    max_entries_per_day: int = 4
    halt_after_consecutive_losses: int = 3
    min_notional: Decimal = Decimal(10)

    def validate(self) -> None:
        if not Decimal(0) < self.risk_per_trade_pct <= Decimal(2):
            raise ValueError("risk_per_trade_pct must be in (0, 2]")
        if not Decimal(0) < self.daily_loss_limit_pct <= Decimal(10):
            raise ValueError("daily_loss_limit_pct must be in (0, 10]")
        if not Decimal(0) < self.drawdown_halt_pct <= Decimal(50):
            raise ValueError("drawdown_halt_pct must be in (0, 50]")
        if not Decimal(0) < self.max_leverage <= Decimal(5):
            raise ValueError("max_leverage must be in (0, 5]")
        if self.max_entries_per_day < 1 or self.halt_after_consecutive_losses < 1:
            raise ValueError("limits must be >= 1")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be > 0")


@dataclass(frozen=True)
class PaperExecution:
    #: observed tier-0 taker fee on xyz:CL (ADR-028), charged on EVERY fill (incl. targets)
    taker_fee_rate: Decimal = OBSERVED_TAKER_FEE
    #: extra adverse slippage on top of walking the live book (latency / queue risk)
    extra_slippage_rate: Decimal = Decimal("0.0002")
    #: assumed slippage of a stop exit, used when sizing (conservative)
    stop_slippage_estimate: Decimal = Decimal("0.0010")
    #: entries refused when the book is wider than this
    max_spread_bps: Decimal = Decimal(15)
    #: book older than this is stale (no entry, no exit decision)
    max_book_age_s: int = 15
    #: an entry may walk the book at most this far from the touch
    max_entry_depth_move: Decimal = Decimal("0.005")
    #: an entry must happen within this many seconds after the signal bar closed
    entry_window_s: int = 180
    #: |hourly funding| above this blocks entries
    max_abs_funding_rate: Decimal = Decimal("0.0005")
    #: latest recorded funding older than this blocks entries (funding data missing)
    max_funding_age_s: int = 2 * 3600
    #: charged against the position for an hour whose funding wasn't recorded in time
    missing_funding_rate: Decimal = Decimal("0.0000125")
    #: wait this long after an hour for its recorded funding before assuming the above
    funding_grace_s: int = 1800
    #: the collector's last closed 30m bar older than this = market data stale
    max_bar_age_s: int = 45 * 60
    #: CME reference-session entry filter (strategy filter, xyz:CL itself trades 24/7)
    session_filter: bool = True
    #: signal bar range above this many ATRs = abnormal volatility
    max_signal_range_atr: Decimal = Decimal(4)
    sz_decimals: int = 3


@dataclass(frozen=True)
class PaperConfig:
    strategy_id: str = "breakout_volume"
    risk: PaperRisk = field(default_factory=PaperRisk)
    execution: PaperExecution = field(default_factory=PaperExecution)

    def as_dict(self) -> dict[str, Any]:
        def conv(d: dict[str, Any]) -> dict[str, Any]:
            return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

        return {
            "paper_only": True,
            "strategy_id": self.strategy_id,
            "risk": conv(asdict(self.risk)),
            "execution": conv(asdict(self.execution)),
        }


def config_from_dict(d: dict[str, Any]) -> PaperConfig:
    def dec(cls: type, raw: dict[str, Any]) -> Any:
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for k, v in raw.items():
            if not hasattr(defaults, k):
                raise ValueError(f"unknown config key {k}")
            cur = getattr(defaults, k)
            kwargs[k] = (
                Decimal(str(v))
                if isinstance(cur, Decimal)
                else (bool(v) if isinstance(cur, bool) else int(v))
            )
        return cls(**kwargs)

    if d.get("paper_only") is not True:
        raise ValueError("paper accounts must be paper_only")
    risk = dec(PaperRisk, d["risk"])
    risk.validate()
    return PaperConfig(
        strategy_id=str(d["strategy_id"]), risk=risk, execution=dec(PaperExecution, d["execution"])
    )


def config_sha256(d: dict[str, Any]) -> str:
    import json

    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
