"""Persistence for candles, data-quality issues, ingestion runs and heartbeats.

Rules enforced here (see docs/DECISIONS.md ADR-014..016):

* Idempotent writes: one row per (coin, interval, open_time); replaying the same
  data changes nothing.
* Closed bars are immutable. They are only ever marked closed from REST data
  (the source of truth). A later REST value that differs is *not* applied; it is
  recorded as ``REVISED_CLOSED_CANDLE`` so the discrepancy is visible.
* Live (WS) updates only move a forming bar forward: an update with lower
  volume / trade count than what is stored is an out-of-order message and is
  ignored.
* Reads for strategy/backtest code go through :func:`closed_candles`, which only
  returns bars that were closed *as of* the given time (no look-ahead).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from autotrader.db.models import (
    CandleRow,
    DataQualityIssue,
    IngestionRun,
    ServiceHeartbeat,
)
from autotrader.market.candles import Candle, Interval, Rejected
from autotrader.market.quality import Finding, Gap, IssueType, find_gaps

Source = Literal["rest", "ws"]

SEVERITY: dict[IssueType, str] = {
    IssueType.GAP: "ERROR",
    IssueType.DUPLICATE_CONFLICT: "WARNING",
    IssueType.REJECTED_CANDLE: "ERROR",
    IssueType.OUTLIER_RETURN: "WARNING",
    IssueType.OUTLIER_RANGE: "WARNING",
    IssueType.REVISED_CLOSED_CANDLE: "WARNING",
    IssueType.OUT_OF_ORDER: "INFO",
    IssueType.STALE_DATA: "WARNING",
}

#: PostgreSQL advisory-lock key that guarantees a single collector writes at a time.
COLLECTOR_LOCK_KEY = 0x4155544F5452  # "AUTOTR"


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    closed_now: int = 0
    ignored_out_of_order: int = 0
    findings: list[Finding] = field(default_factory=list)


def _values(row: CandleRow) -> tuple[Any, ...]:
    return (row.open, row.high, row.low, row.close, row.volume, row.trades)


def _cvalues(c: Candle) -> tuple[Any, ...]:
    return (c.open, c.high, c.low, c.close, c.volume, c.trades)


def upsert_candles(
    session: Session,
    candles: Sequence[Candle],
    *,
    source: Source,
    now: datetime,
    run_id: int | None = None,
) -> UpsertStats:
    """Idempotently write bars of ONE coin/interval. See module docstring for rules."""
    stats = UpsertStats()
    if not candles:
        return stats
    coin, interval = candles[0].coin, candles[0].interval
    if any(c.coin != coin or c.interval != interval for c in candles):
        raise ValueError("upsert_candles expects a single coin/interval per call")

    times = [c.open_time for c in candles]
    existing = {
        _utc(r.open_time): r
        for r in session.scalars(
            select(CandleRow).where(
                CandleRow.coin == coin,
                CandleRow.interval == interval.value,
                CandleRow.open_time >= min(times),
                CandleRow.open_time <= max(times),
            )
        )
    }
    for c in candles:
        closed = source == "rest" and c.is_closed_at(now)
        row = existing.get(c.open_time)
        if row is None:
            row = CandleRow(
                coin=coin,
                interval=interval.value,
                open_time=c.open_time,
                close_time=c.close_time,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                trades=c.trades,
                is_closed=closed,
                source=source,
                first_seen_at=now,
                updated_at=now,
                closed_at=now if closed else None,
                ingestion_run_id=run_id,
            )
            session.add(row)
            existing[c.open_time] = row
            stats.inserted += 1
            stats.closed_now += int(closed)
            continue

        same = _values(row) == _cvalues(c)
        if row.is_closed:
            if same:
                stats.unchanged += 1
            elif source == "rest":
                stats.unchanged += 1
                stats.findings.append(
                    Finding(
                        IssueType.REVISED_CLOSED_CANDLE,
                        c.open_time,
                        f"exchange now reports close={c.close} volume={c.volume}; stored "
                        f"close={row.close} volume={row.volume} kept (closed bars are immutable)",
                    )
                )
            else:
                # Late live message for a bar REST already finalized: ignore.
                stats.ignored_out_of_order += 1
            continue

        # Forming bar.
        if source == "ws" and (
            c.volume < row.volume
            or (c.trades is not None and row.trades is not None and c.trades < row.trades)
        ):
            stats.ignored_out_of_order += 1
            continue
        if same and not closed:
            stats.unchanged += 1
            continue
        row.open, row.high, row.low, row.close = c.open, c.high, c.low, c.close
        row.volume, row.trades = c.volume, c.trades
        row.updated_at = now
        row.source = source  # last writer of a forming bar
        if source == "rest":
            row.ingestion_run_id = run_id
        if closed:
            row.is_closed = True
            row.closed_at = now
            stats.closed_now += 1
        stats.updated += 1
    session.flush()
    return stats


def _utc(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- issues


def record_issue(
    session: Session,
    *,
    coin: str,
    interval: Interval,
    issue_type: IssueType,
    open_time: datetime,
    detail: str,
    now: datetime,
    range_end: datetime | None = None,
    missing_bars: int | None = None,
    raw: Any = None,
) -> DataQualityIssue:
    """Insert or re-detect an issue (unique per coin/interval/type/bar)."""
    row = session.scalar(
        select(DataQualityIssue).where(
            DataQualityIssue.coin == coin,
            DataQualityIssue.interval == interval.value,
            DataQualityIssue.issue_type == issue_type.value,
            DataQualityIssue.open_time == open_time,
        )
    )
    if row is None:
        row = DataQualityIssue(
            coin=coin,
            interval=interval.value,
            issue_type=issue_type.value,
            open_time=open_time,
            range_end=range_end,
            missing_bars=missing_bars,
            severity=SEVERITY[issue_type],
            detail=detail,
            raw=raw,
            first_detected_at=now,
            last_detected_at=now,
            occurrences=1,
        )
        session.add(row)
    else:
        row.last_detected_at = now
        row.occurrences += 1
        row.detail = detail
        row.range_end = range_end
        row.missing_bars = missing_bars
        row.resolved_at = None
    session.flush()
    return row


def record_findings(
    session: Session, coin: str, interval: Interval, findings: Iterable[Finding], now: datetime
) -> int:
    n = 0
    for f in findings:
        record_issue(
            session,
            coin=coin,
            interval=interval,
            issue_type=f.issue_type,
            open_time=f.open_time,
            detail=f.detail,
            now=now,
        )
        n += 1
    return n


def record_rejections(
    session: Session, coin: str, interval: Interval, rejected: Iterable[Rejected], now: datetime
) -> int:
    n = 0
    for r in rejected:
        # Bars that can't even be timed are anchored at epoch 0 so they stay unique per reason.
        from datetime import UTC

        anchor = r.open_time or datetime.fromtimestamp(0, tz=UTC)
        record_issue(
            session,
            coin=coin,
            interval=interval,
            issue_type=IssueType.REJECTED_CANDLE,
            open_time=anchor,
            detail=f"{r.reason}: {r.detail}",
            now=now,
            raw=_jsonable(r.raw),
        )
        n += 1
    return n


def _jsonable(raw: Any) -> Any:
    try:
        json.dumps(raw)
        return raw
    except (TypeError, ValueError):
        return repr(raw)[:2000]


def closed_open_times(session: Session, coin: str, interval: Interval) -> list[datetime]:
    return [
        _utc(t)
        for t in session.scalars(
            select(CandleRow.open_time)
            .where(
                CandleRow.coin == coin,
                CandleRow.interval == interval.value,
                CandleRow.is_closed.is_(True),
            )
            .order_by(CandleRow.open_time)
        )
    ]


def refresh_gaps(session: Session, coin: str, interval: Interval, now: datetime) -> list[Gap]:
    """Detect gaps across all stored closed bars; record new ones, resolve filled ones."""
    gaps = find_gaps(closed_open_times(session, coin, interval), interval)
    current = {g.first_missing for g in gaps}
    for g in gaps:
        record_issue(
            session,
            coin=coin,
            interval=interval,
            issue_type=IssueType.GAP,
            open_time=g.first_missing,
            range_end=g.resumes_at,
            missing_bars=g.missing_bars,
            detail=f"{g.missing_bars} missing {interval} bar(s); not filled",
            now=now,
        )
    for row in session.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.coin == coin,
            DataQualityIssue.interval == interval.value,
            DataQualityIssue.issue_type == IssueType.GAP.value,
            DataQualityIssue.resolved_at.is_(None),
        )
    ):
        if _utc(row.open_time) not in current:
            row.resolved_at = now
    session.flush()
    return gaps


# --------------------------------------------------------------------------- reads


def closed_candles(
    session: Session,
    coin: str,
    interval: Interval,
    *,
    as_of: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> list[CandleRow]:
    """Bars that were CLOSED as of ``as_of`` - the only read strategies may use.

    A bar is returned only if ``is_closed`` and ``close_time <= as_of``, so a
    backtest evaluating time T can never see a bar that ends after T.
    """
    stmt = select(CandleRow).where(
        CandleRow.coin == coin,
        CandleRow.interval == interval.value,
        CandleRow.is_closed.is_(True),
        CandleRow.close_time <= as_of,
    )
    if start is not None:
        stmt = stmt.where(CandleRow.open_time >= start)
    if end is not None:
        stmt = stmt.where(CandleRow.open_time < end)
    if limit is not None:
        # Most recent `limit` bars, returned in chronological order.
        stmt = stmt.order_by(CandleRow.open_time.desc()).limit(limit)
        return list(reversed(list(session.scalars(stmt))))
    return list(session.scalars(stmt.order_by(CandleRow.open_time)))


def forming_candle(session: Session, coin: str, interval: Interval) -> CandleRow | None:
    """The latest not-yet-closed bar (display only - never for strategy input)."""
    return session.scalar(
        select(CandleRow)
        .where(
            CandleRow.coin == coin,
            CandleRow.interval == interval.value,
            CandleRow.is_closed.is_(False),
        )
        .order_by(CandleRow.open_time.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class Coverage:
    interval: Interval
    closed_count: int
    first_open_time: datetime | None
    last_open_time: datetime | None
    expected_bars: int
    missing_bars: int
    open_gaps: int
    open_issues: dict[str, int]
    forming_open_time: datetime | None


def coverage(session: Session, coin: str, interval: Interval) -> Coverage:
    q = select(func.count(), func.min(CandleRow.open_time), func.max(CandleRow.open_time)).where(
        CandleRow.coin == coin,
        CandleRow.interval == interval.value,
        CandleRow.is_closed.is_(True),
    )
    count, first_raw, last_raw = session.execute(q).one()
    first = _utc(first_raw) if first_raw else None
    last = _utc(last_raw) if last_raw else None
    expected = int((last - first) / interval.delta) + 1 if first and last else 0
    issues = dict(
        session.execute(
            select(DataQualityIssue.issue_type, func.count())
            .where(
                DataQualityIssue.coin == coin,
                DataQualityIssue.interval == interval.value,
                DataQualityIssue.resolved_at.is_(None),
            )
            .group_by(DataQualityIssue.issue_type)
        ).all()
    )
    forming = forming_candle(session, coin, interval)
    return Coverage(
        interval=interval,
        closed_count=count,
        first_open_time=first,
        last_open_time=last,
        expected_bars=expected,
        missing_bars=max(expected - count, 0),
        open_gaps=int(issues.get(IssueType.GAP.value, 0)),
        open_issues={str(k): int(v) for k, v in issues.items()},
        forming_open_time=_utc(forming.open_time) if forming else None,
    )


def list_issues(
    session: Session,
    coin: str,
    interval: Interval | None = None,
    *,
    include_resolved: bool = False,
    limit: int = 200,
) -> list[DataQualityIssue]:
    stmt = select(DataQualityIssue).where(DataQualityIssue.coin == coin)
    if interval is not None:
        stmt = stmt.where(DataQualityIssue.interval == interval.value)
    if not include_resolved:
        stmt = stmt.where(DataQualityIssue.resolved_at.is_(None))
    stmt = stmt.order_by(DataQualityIssue.last_detected_at.desc(), DataQualityIssue.id.desc())
    return list(session.scalars(stmt.limit(limit)))


# --------------------------------------------------------------------------- runs


def start_run(
    session: Session,
    *,
    coin: str,
    interval: Interval,
    kind: str,
    requested_start: datetime,
    requested_end: datetime,
    now: datetime,
) -> IngestionRun:
    run = IngestionRun(
        coin=coin,
        interval=interval.value,
        kind=kind,
        status="RUNNING",
        requested_start=requested_start,
        requested_end=requested_end,
        started_at=now,
    )
    session.add(run)
    session.flush()
    return run


def raw_digest(raw: Any) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def latest_runs(session: Session, coin: str, limit: int = 20) -> list[IngestionRun]:
    return list(
        session.scalars(
            select(IngestionRun)
            .where(IngestionRun.coin == coin)
            .order_by(IngestionRun.id.desc())
            .limit(limit)
        )
    )


# --------------------------------------------------------------------------- heartbeat


def write_heartbeat(session: Session, service: str, **fields: Any) -> ServiceHeartbeat:
    row = session.get(ServiceHeartbeat, service)
    if row is None:
        row = ServiceHeartbeat(service=service, **fields)
        session.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    session.flush()
    return row


def get_heartbeat(session: Session, service: str) -> ServiceHeartbeat | None:
    return session.get(ServiceHeartbeat, service)


def try_collector_lock(session: Session) -> bool:
    """Session-level advisory lock so two collectors never write concurrently.

    Returns True on non-PostgreSQL backends (tests).
    """
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": COLLECTOR_LOCK_KEY}).scalar()
    )
