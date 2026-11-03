"""REST ingestion: historical backfill, gap fill and closed-bar finalization.

All REST data goes through the PAPER-only :class:`~autotrader.common.http.InfoClient`
(``candleSnapshot`` is on the Info allow-list). Every call is recorded as an
``ingestion_runs`` row with what was requested and what actually came back.

Hyperliquid serves at most the ~5,000 most recent bars per interval; older
history is simply not available from this API. We request everything and
report the real range - nothing is synthesized.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.http import InfoApiError
from autotrader.db.market_data import (
    raw_digest,
    record_findings,
    record_rejections,
    refresh_gaps,
    start_run,
    upsert_candles,
)
from autotrader.db.models import IngestionRun
from autotrader.market.candles import Candle, Interval, Rejected, dt_to_ms, parse_candle
from autotrader.market.quality import dedupe, find_outliers

log = logging.getLogger("autotrader.ingest")

EPOCH = datetime.fromtimestamp(0, tz=UTC)
#: Never ask for more bars than this in one request (API cap is ~5,000).
MAX_BARS_PER_REQUEST = 4_000


class InfoSource(Protocol):
    def info(self, payload: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class IngestResult:
    run_id: int
    ok: bool
    received: int
    inserted: int
    updated: int
    unchanged: int
    rejected: int
    closed_now: int
    first_open_time: datetime | None
    last_open_time: datetime | None
    gaps: int
    error: str | None = None


def fetch_snapshot(
    source: InfoSource, coin: str, interval: Interval, start: datetime, end: datetime
) -> Any:
    return source.info(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval.value,
                "startTime": dt_to_ms(start),
                "endTime": dt_to_ms(end),
            },
        }
    )


def ingest_rest(
    sessions: sessionmaker[Session],
    source: InfoSource,
    *,
    coin: str,
    interval: Interval,
    kind: str,
    start: datetime,
    end: datetime,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> IngestResult:
    """Fetch [start, end] via REST and store it. Never raises for API/data problems."""
    with sessions.begin() as s:
        run = start_run(
            s,
            coin=coin,
            interval=interval,
            kind=kind,
            requested_start=start,
            requested_end=end,
            now=clock(),
        )
        run_id = run.id

    try:
        raw = fetch_snapshot(source, coin, interval, start, end)
    except InfoApiError as exc:
        return _fail(sessions, run_id, f"API_UNAVAILABLE: {exc}", clock)
    if not isinstance(raw, list):
        return _fail(sessions, run_id, "MALFORMED_RESPONSE: candleSnapshot is not a list", clock)

    parsed = [parse_candle(r, coin=coin, interval=interval) for r in raw]
    good = [p for p in parsed if isinstance(p, Candle)]
    bad = [p for p in parsed if isinstance(p, Rejected)]
    candles, dup_findings = dedupe(good)
    now = clock()

    with sessions.begin() as s:
        stats = upsert_candles(s, candles, source="rest", now=now, run_id=run_id)
        closed = [c for c in candles if c.is_closed_at(now)]
        record_findings(s, coin, interval, dup_findings, now)
        record_findings(s, coin, interval, stats.findings, now)
        record_findings(s, coin, interval, find_outliers(closed, interval), now)
        record_rejections(s, coin, interval, bad, now)
        gaps = refresh_gaps(s, coin, interval, now)
        run_row = s.get(IngestionRun, run_id)
        assert run_row is not None
        run_row.status = "OK"
        run_row.received = len(raw)
        run_row.inserted = stats.inserted
        run_row.updated = stats.updated
        run_row.unchanged = stats.unchanged
        run_row.rejected = len(bad)
        run_row.first_open_time = candles[0].open_time if candles else None
        run_row.last_open_time = candles[-1].open_time if candles else None
        run_row.raw_sha256 = raw_digest(raw)
        run_row.finished_at = clock()

    log.info(
        "rest ingest done",
        extra={
            "coin": coin,
            "interval": interval.value,
            "kind": kind,
            "received": len(raw),
            "inserted": stats.inserted,
            "updated": stats.updated,
            "rejected": len(bad),
            "gaps": len(gaps),
        },
    )
    return IngestResult(
        run_id=run_id,
        ok=True,
        received=len(raw),
        inserted=stats.inserted,
        updated=stats.updated,
        unchanged=stats.unchanged,
        rejected=len(bad),
        closed_now=stats.closed_now,
        first_open_time=candles[0].open_time if candles else None,
        last_open_time=candles[-1].open_time if candles else None,
        gaps=len(gaps),
    )


def _fail(
    sessions: sessionmaker[Session], run_id: int, error: str, clock: Callable[[], datetime]
) -> IngestResult:
    log.error("rest ingest failed", extra={"run_id": run_id, "error": error})
    with sessions.begin() as s:
        run_row = s.get(IngestionRun, run_id)
        assert run_row is not None
        run_row.status = "FAILED"
        run_row.error = error
        run_row.finished_at = clock()
    return IngestResult(run_id, False, 0, 0, 0, 0, 0, 0, None, None, 0, error)


def backfill(
    sessions: sessionmaker[Session],
    source: InfoSource,
    *,
    coin: str,
    interval: Interval,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> IngestResult:
    """Ask for ALL history; the API returns what it has (the most recent ~5,000 bars)."""
    return ingest_rest(
        sessions,
        source,
        coin=coin,
        interval=interval,
        kind="backfill",
        start=EPOCH,
        end=clock(),
        clock=clock,
    )


def sync_since(
    sessions: sessionmaker[Session],
    source: InfoSource,
    *,
    coin: str,
    interval: Interval,
    last_closed_open_time: datetime | None,
    kind: str,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> list[IngestResult]:
    """Fetch everything after the last closed bar (finalize + gap fill), chunked."""
    now = clock()
    if last_closed_open_time is None:
        return [backfill(sessions, source, coin=coin, interval=interval, clock=clock)]
    results: list[IngestResult] = []
    start = last_closed_open_time
    chunk = interval.delta * MAX_BARS_PER_REQUEST
    while start < now:
        end = min(start + chunk, now)
        results.append(
            ingest_rest(
                sessions,
                source,
                coin=coin,
                interval=interval,
                kind=kind,
                start=start,
                end=end,
                clock=clock,
            )
        )
        if not results[-1].ok:
            break
        start = end
    return results


def needs_finalize(
    last_closed_open_time: datetime | None, interval: Interval, now: datetime
) -> bool:
    """True when at least one more bar should have closed since the last stored one."""
    from autotrader.market.candles import CLOSE_GRACE

    if last_closed_open_time is None:
        return True
    next_close = last_closed_open_time + 2 * interval.delta  # close of the following bar
    return now >= next_close + CLOSE_GRACE
