"""Honest data-coverage report (used by the CLI and the API)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from autotrader.db.market_data import coverage, latest_runs, list_issues
from autotrader.market.candles import Interval


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def coverage_dict(session: Session, coin: str, interval: Interval) -> dict[str, Any]:
    cov = coverage(session, coin, interval)
    days = (
        (cov.last_open_time - cov.first_open_time).total_seconds() / 86400
        if cov.first_open_time and cov.last_open_time
        else 0.0
    )
    return {
        "interval": interval.value,
        "closed_candles": cov.closed_count,
        "first_open_time": _iso(cov.first_open_time),
        "last_open_time": _iso(cov.last_open_time),
        "span_days": round(days, 2),
        "expected_bars_in_span": cov.expected_bars,
        "missing_bars": cov.missing_bars,
        "open_gaps": cov.open_gaps,
        "open_issues": cov.open_issues,
        "forming_candle_open_time": _iso(cov.forming_open_time),
    }


def build_data_report(session: Session, coin: str) -> dict[str, Any]:
    runs = latest_runs(session, coin, limit=10)
    return {
        "coin": coin,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "source": "Hyperliquid mainnet public Info API (candleSnapshot) + WS candle stream",
        "history_limit_note": (
            "The API serves only the most recent ~5,000 bars per interval; "
            "older history is not available from this source and is not synthesized."
        ),
        "intervals": [coverage_dict(session, coin, iv) for iv in Interval],
        "open_issues": [
            {
                "interval": i.interval,
                "type": i.issue_type,
                "severity": i.severity,
                "open_time": _iso(i.open_time),
                "missing_bars": i.missing_bars,
                "detail": i.detail,
                "occurrences": i.occurrences,
            }
            for i in list_issues(session, coin, limit=50)
        ],
        "recent_ingestion_runs": [
            {
                "id": r.id,
                "interval": r.interval,
                "kind": r.kind,
                "status": r.status,
                "received": r.received,
                "inserted": r.inserted,
                "updated": r.updated,
                "rejected": r.rejected,
                "first_open_time": _iso(r.first_open_time),
                "last_open_time": _iso(r.last_open_time),
                "started_at": _iso(r.started_at),
                "error": r.error,
            }
            for r in runs
        ],
    }
