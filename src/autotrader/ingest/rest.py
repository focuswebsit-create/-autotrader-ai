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
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError
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
    write_lock: AbstractContextManager[Any] | None = None,
) -> IngestResult:
    """Fetch [start, end] via REST and store it. Never raises for API/data/DB problems.

    * A bar is marked closed only if it was already closed (end + grace) when the
      request was SENT. Measuring at response time would freeze a partial bar
      whenever the response is slow (review finding H1).
    * ``write_lock`` (if given) is held only around database writes, never
      during the network call, so a slow API cannot block other writers (M2).
    """
    lock: AbstractContextManager[Any] = write_lock or nullcontext()
    try:
        with lock, sessions.begin() as s:
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
    except SQLAlchemyError as exc:
        log.error("rest ingest not started: database", extra={"error": type(exc).__name__})
        return IngestResult(
            0, False, 0, 0, 0, 0, 0, 0, None, None, 0, f"DATABASE: {type(exc).__name__}"
        )

    requested_at = clock()
    try:
        raw = fetch_snapshot(source, coin, interval, start, end)
    except InfoApiError as exc:
        return _fail(sessions, run_id, f"API_UNAVAILABLE: {exc}", clock, lock)
    if not isinstance(raw, list):
        return _fail(
            sessions, run_id, "MALFORMED_RESPONSE: candleSnapshot is not a list", clock, lock
        )

    parsed = [parse_candle(r, coin=coin, interval=interval) for r in raw]
    good = [p for p in parsed if isinstance(p, Candle)]
    bad = [p for p in parsed if isinstance(p, Rejected)]
    candles, dup_findings = dedupe(good)
    now = clock()

    try:
        with lock, sessions.begin() as s:
            stats = upsert_candles(
                s, candles, source="rest", now=now, run_id=run_id, closed_as_of=requested_at
            )
            closed = [c for c in candles if c.is_closed_at(requested_at)]
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
    except SQLAlchemyError as exc:
        # Nothing of this batch was stored (single transaction); the run must not
        # stay RUNNING forever (review finding L3).
        return _fail(sessions, run_id, f"DATABASE: {type(exc).__name__}", clock, lock)

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
    sessions: sessionmaker[Session],
    run_id: int,
    error: str,
    clock: Callable[[], datetime],
    lock: AbstractContextManager[Any] | None = None,
) -> IngestResult:
    log.error("rest ingest failed", extra={"run_id": run_id, "error": error})
    try:
        with lock or nullcontext(), sessions.begin() as s:
            run_row = s.get(IngestionRun, run_id)
            if run_row is not None:
                run_row.status = "FAILED"
                run_row.error = error
                run_row.finished_at = clock()
    except SQLAlchemyError:
        log.error("could not mark ingestion run FAILED (database)", extra={"run_id": run_id})
    return IngestResult(run_id, False, 0, 0, 0, 0, 0, 0, None, None, 0, error)


def backfill(
    sessions: sessionmaker[Session],
    source: InfoSource,
    *,
    coin: str,
    interval: Interval,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    write_lock: AbstractContextManager[Any] | None = None,
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
        write_lock=write_lock,
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
    write_lock: AbstractContextManager[Any] | None = None,
) -> list[IngestResult]:
    """Fetch everything after the last closed bar (finalize + gap fill), chunked."""
    now = clock()
    if last_closed_open_time is None:
        return [
            backfill(
                sessions, source, coin=coin, interval=interval, clock=clock, write_lock=write_lock
            )
        ]
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
                write_lock=write_lock,
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
