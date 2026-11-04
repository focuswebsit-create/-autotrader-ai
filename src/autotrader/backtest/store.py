"""Load backtest inputs from the database and persist immutable run records."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autotrader.backtest.research import MarketData
from autotrader.db.market_data import closed_candles
from autotrader.db.models import BacktestRun, BacktestTrade, CandleRow
from autotrader.ingest.funding import load_funding
from autotrader.market.candles import Interval
from autotrader.strategy.base import Bar


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _bar(r: CandleRow) -> Bar:
    return Bar(
        _utc(r.open_time), _utc(r.close_time), r.open, r.high, r.low, r.close, r.volume, r.trades
    )


def load_market_data(session: Session, coin: str, *, as_of: datetime) -> MarketData:
    """Closed bars only (``closed_candles(as_of=...)``) plus recorded funding."""
    m30 = [_bar(r) for r in closed_candles(session, coin, Interval.M30, as_of=as_of)]
    h1 = [_bar(r) for r in closed_candles(session, coin, Interval.H1, as_of=as_of)]
    funding = load_funding(session, coin, as_of=as_of)
    return MarketData(
        coin=coin,
        m30=m30,
        h1=h1,
        funding=funding,
        source="PostgreSQL closed candles (Hyperliquid candleSnapshot/WS, REST-confirmed) + "
        "funding_rates (Hyperliquid fundingHistory)",
        as_of=as_of,
    )


def code_version() -> str:
    """Git commit of the code that produced a run (env override for containers)."""
    env = os.environ.get("AUTOTRADER_GIT_SHA")
    if env:
        return env[:64]
    try:
        root = Path(__file__).resolve().parents[3]
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return f"{out}{'-dirty' if dirty else ''}"[:64]
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def save_run(session: Session, report: dict[str, Any], *, now: datetime) -> BacktestRun:
    s = report["strategy"]
    window = report["trade_window"]
    run = BacktestRun(
        coin=report["data"]["coin"],
        strategy_id=s["id"],
        strategy_version=s["version"],
        params_hash=report["params_hash"],
        params=report["params"],
        code_version=report["code"]["commit"],
        data_fingerprint=report["data"]["fingerprint_sha256"],
        data_start=datetime.fromisoformat(window["start"]),
        data_end=datetime.fromisoformat(window["end"]),
        verdict=report["verdict"]["status"],
        summary=report["primary"]["summary"],
        report=report,
        report_sha256=report["report_sha256"],
        created_at=now,
    )
    session.add(run)
    session.flush()
    for t in report["primary"]["trades"]:
        session.add(
            BacktestTrade(
                run_id=run.id,
                trade_no=t["id"],
                side=t["side"],
                signal_time=datetime.fromisoformat(t["signal_time"]),
                entry_time=datetime.fromisoformat(t["entry_time"]),
                entry_price=Decimal(t["entry_price"]),
                exit_time=datetime.fromisoformat(t["exit_time"]),
                exit_price=Decimal(t["exit_price"]),
                exit_reason=t["exit_reason"],
                size=Decimal(t["size"]),
                net_pnl=Decimal(t["net_pnl"]),
                r_multiple=Decimal(t["r_multiple"]),
                details=t,
            )
        )
    session.flush()
    return run


def run_header(r: BacktestRun) -> dict[str, Any]:
    s = r.summary
    return {
        "id": r.id,
        "created_at": _utc(r.created_at).isoformat(),
        "coin": r.coin,
        "strategy_id": r.strategy_id,
        "strategy_version": r.strategy_version,
        "title_he": (r.report.get("strategy") or {}).get("title_he"),
        "params_hash": r.params_hash,
        "code_version": r.code_version,
        "data_fingerprint": r.data_fingerprint,
        "data_start": _utc(r.data_start).isoformat(),
        "data_end": _utc(r.data_end).isoformat(),
        "verdict": r.verdict,
        "trades": s.get("trades"),
        "net_pnl": s.get("net_pnl"),
        "win_rate": s.get("win_rate"),
        "profit_factor": s.get("profit_factor"),
        "max_drawdown_pct": s.get("max_drawdown_pct"),
        "expectancy_r": s.get("expectancy_r"),
    }


def list_runs(session: Session, coin: str, limit: int = 50) -> list[BacktestRun]:
    return list(
        session.scalars(
            select(BacktestRun)
            .where(BacktestRun.coin == coin)
            .order_by(BacktestRun.id.desc())
            .limit(limit)
        )
    )


def get_run(session: Session, run_id: int) -> BacktestRun | None:
    return session.get(BacktestRun, run_id)
