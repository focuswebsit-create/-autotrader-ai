"""Controlled repair of closed candles that differ from the exchange (audited).

Never runs automatically. Usage (admin / owner DB user only - the service
roles are refused by the database trigger, migration 0005):

    autotrader repair-candles                       # dry-run: report differences
    autotrader repair-candles --apply --ticket QA-1 --backup-dir ./backups

On --apply: the rows are backed up to a JSON file BEFORE any change; all
corrections happen in ONE transaction under the single-writer lock; the DB
trigger writes before/after into the append-only audit_log for every row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from autotrader.db.market_data import record_issue
from autotrader.db.models import CandleRow
from autotrader.ingest.rest import EPOCH, InfoSource, fetch_snapshot
from autotrader.market.candles import Candle, Interval, parse_candle
from autotrader.market.quality import IssueType

FIELDS = ("open", "high", "low", "close", "volume", "trades")


@dataclass(frozen=True)
class Difference:
    interval: str
    open_time: str
    stored: dict[str, str | None]
    exchange: dict[str, str | None]


def _vals(obj: Any) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for f in FIELDS:
        v = getattr(obj, f)
        out[f] = None if v is None else (format(v, "f") if isinstance(v, Decimal) else str(v))
    return out


def _canon(d: dict[str, str | None]) -> dict[str, Any]:
    return {k: (Decimal(v) if v is not None and k != "trades" else v) for k, v in d.items()}


def find_differences(
    session: Session, source: InfoSource, coin: str, interval: Interval
) -> tuple[list[Difference], dict[datetime, Candle], int]:
    """Compare stored CLOSED bars with bars that were closed when the request was sent."""
    sent = datetime.now(tz=UTC)
    raw = fetch_snapshot(source, coin, interval, EPOCH, sent)
    exchange: dict[datetime, Candle] = {}
    for r in raw if isinstance(raw, list) else []:
        c = parse_candle(r, coin=coin, interval=interval)
        if isinstance(c, Candle) and c.is_closed_at(sent):
            exchange[c.open_time] = c
    rows = session.scalars(
        select(CandleRow).where(
            CandleRow.coin == coin,
            CandleRow.interval == interval.value,
            CandleRow.is_closed.is_(True),
            CandleRow.open_time >= min(exchange) if exchange else CandleRow.id < 0,
        )
    ).all()
    diffs: list[Difference] = []
    compared = 0
    for row in rows:
        ot = row.open_time if row.open_time.tzinfo else row.open_time.replace(tzinfo=UTC)
        ex = exchange.get(ot)
        if ex is None:
            continue
        compared += 1
        if _canon(_vals(row)) != _canon(_vals(ex)):
            diffs.append(Difference(interval.value, ot.isoformat(), _vals(row), _vals(ex)))
    return diffs, exchange, compared


def apply_corrections(
    sessions: sessionmaker[Session],
    coin: str,
    interval: Interval,
    diffs: list[Difference],
    exchange: dict[datetime, Candle],
    *,
    ticket: str,
    backup_dir: Path,
) -> Path:
    """Back up, then correct all differing rows in ONE audited transaction."""
    if not ticket.strip():
        raise ValueError("a correction ticket is required")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"candles-backup-{coin.replace(':', '_')}-{interval.value}-{stamp}.json"
    backup.write_text(
        json.dumps(
            {
                "ticket": ticket,
                "coin": coin,
                "interval": interval.value,
                "rows": [d.__dict__ for d in diffs],
            },
            indent=1,
        )
    )
    now = datetime.now(tz=UTC)
    with sessions.begin() as s:
        s.execute(
            text("SELECT set_config('autotrader.correction_ticket', :t, true)"), {"t": ticket}
        )
        for d in diffs:
            ot = datetime.fromisoformat(d.open_time)
            ex = exchange[ot]
            row = s.scalar(
                select(CandleRow).where(
                    CandleRow.coin == coin,
                    CandleRow.interval == interval.value,
                    CandleRow.open_time == ot,
                )
            )
            assert row is not None and row.is_closed
            row.open, row.high, row.low, row.close = ex.open, ex.high, ex.low, ex.close
            row.volume, row.trades = ex.volume, ex.trades
            row.source = "rest"
            row.updated_at = now
            record_issue(
                s,
                coin=coin,
                interval=interval,
                issue_type=IssueType.REVISED_CLOSED_CANDLE,
                open_time=ot,
                detail=f"CORRECTED under ticket {ticket}; backup {backup.name}",
                now=now,
                raw={"stored": d.stored, "exchange": d.exchange},
            )
    return backup
