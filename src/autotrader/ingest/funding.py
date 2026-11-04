"""Recorded funding history for the target market (Hyperliquid ``fundingHistory``).

Public, unsigned Info request (on the PAPER-only allow-list). The API returns
at most 500 records per call, so we page forward by time. Records are stored
append-only, one per (coin, funding hour). Nothing is interpolated: an hour the
exchange did not report stays missing and the backtest charges an explicit,
disclosed adverse assumption for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autotrader.db.models import FundingRate
from autotrader.ingest.rest import InfoSource
from autotrader.market.candles import dt_to_ms, ms_to_dt

log = logging.getLogger("autotrader.ingest.funding")

PAGE_LIMIT = 500
MAX_PAGES = 200


@dataclass(frozen=True)
class FundingRecord:
    time: datetime
    hour: datetime
    rate: Decimal
    premium: Decimal | None


@dataclass
class FundingSyncResult:
    received: int = 0
    inserted: int = 0
    rejected: int = 0
    pages: int = 0
    first: datetime | None = None
    last: datetime | None = None


def parse_funding(raw: Any, coin: str) -> FundingRecord | None:
    """Strictly parse one record; returns None for anything malformed or out of range."""
    if not isinstance(raw, dict) or raw.get("coin") != coin:
        return None
    t = raw.get("time")
    if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
        return None
    try:
        rate = Decimal(str(raw["fundingRate"]))
        prem_raw = raw.get("premium")
        premium = None if prem_raw is None else Decimal(str(prem_raw))
    except (KeyError, InvalidOperation, ValueError):
        return None
    if not rate.is_finite() or not -1 < rate < 1:
        return None
    if premium is not None and not premium.is_finite():
        return None
    ts = ms_to_dt(t)
    return FundingRecord(ts, ts.replace(minute=0, second=0, microsecond=0), rate, premium)


def fetch_funding(
    source: InfoSource, coin: str, start: datetime, end: datetime | None = None
) -> tuple[list[FundingRecord], int, int]:
    """Page through fundingHistory from ``start``. Returns (records, rejected, pages)."""
    out: dict[datetime, FundingRecord] = {}
    rejected = 0
    cursor = dt_to_ms(start)
    end_ms = dt_to_ms(end) if end else None
    pages = 0
    while pages < MAX_PAGES:
        payload: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": cursor}
        if end_ms is not None:
            payload["endTime"] = end_ms
        page = source.info(payload)
        pages += 1
        if not isinstance(page, list) or not page:
            break
        last_ms = cursor
        for raw in page:
            rec = parse_funding(raw, coin)
            if rec is None:
                rejected += 1
                continue
            out.setdefault(rec.hour, rec)
            last_ms = max(last_ms, dt_to_ms(rec.time))
        if len(page) < PAGE_LIMIT or last_ms + 1 <= cursor:
            break
        cursor = last_ms + 1
    return [out[h] for h in sorted(out)], rejected, pages


def sync_funding(
    session: Session, source: InfoSource, coin: str, *, start: datetime, now: datetime
) -> FundingSyncResult:
    """Fetch funding newer than what is stored (or from ``start``) and insert new hours."""
    latest = session.scalar(select(func.max(FundingRate.hour)).where(FundingRate.coin == coin))
    if latest is not None:
        latest = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
        start = max(start, latest + timedelta(minutes=1))
    records, rejected, pages = fetch_funding(source, coin, start)
    existing = set(
        _utc(h)
        for h in session.scalars(
            select(FundingRate.hour).where(FundingRate.coin == coin, FundingRate.hour >= start)
        )
    )
    res = FundingSyncResult(received=len(records), rejected=rejected, pages=pages)
    for r in records:
        if r.hour in existing:
            continue
        session.add(
            FundingRate(
                coin=coin,
                time=r.time,
                hour=r.hour,
                rate=r.rate,
                premium=r.premium,
                source="hyperliquid:fundingHistory",
                received_at=now,
            )
        )
        res.inserted += 1
    if records:
        res.first, res.last = records[0].hour, records[-1].hour
    session.flush()
    log.info("funding sync", extra={"coin": coin, "inserted": res.inserted, "pages": pages})
    return res


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def load_funding(
    session: Session, coin: str, *, start: datetime | None = None, as_of: datetime | None = None
) -> dict[datetime, Decimal]:
    stmt = select(FundingRate.hour, FundingRate.rate).where(FundingRate.coin == coin)
    if start is not None:
        stmt = stmt.where(FundingRate.hour >= start)
    if as_of is not None:
        stmt = stmt.where(FundingRate.time <= as_of)
    return {_utc(h): r for h, r in session.execute(stmt.order_by(FundingRate.hour))}
