"""REST backfill / sync against recorded mainnet responses, plus failure paths."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.http import InfoApiError
from autotrader.db.market_data import coverage, list_issues
from autotrader.db.models import CandleRow, IngestionRun
from autotrader.ingest.rest import (
    EPOCH,
    MAX_BARS_PER_REQUEST,
    backfill,
    needs_finalize,
    sync_since,
)
from autotrader.market.candles import Interval
from tests.conftest import RECORDED_AT, Clock

COIN = "xyz:CL"


class FakeInfo:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def info(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response(payload) if callable(self.response) else self.response


def _count(sessions: sessionmaker[Session]) -> int:
    with sessions() as s:
        return int(s.scalar(select(func.count()).select_from(CandleRow)) or 0)


def test_backfill_requests_everything_and_records_provenance(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    info = FakeInfo(snapshot_30m)
    r = backfill(sqlite_sessions, info, coin=COIN, interval=Interval.M30, clock=Clock())
    assert r.ok and r.received == 201 and r.inserted == 201 and r.gaps == 0
    req = info.calls[0]
    assert req["type"] == "candleSnapshot"
    assert req["req"] == {
        "coin": COIN,
        "interval": "30m",
        "startTime": 0,  # ask for ALL history; the API decides what exists
        "endTime": int(RECORDED_AT.timestamp() * 1000),
    }
    with sqlite_sessions() as s:
        run = s.scalars(select(IngestionRun)).one()
        assert run.status == "OK" and run.kind == "backfill"
        assert run.requested_start.replace(tzinfo=None) == EPOCH.replace(tzinfo=None)
        assert run.received == 201 and run.raw_sha256 and len(run.raw_sha256) == 64
        cov = coverage(s, COIN, Interval.M30)
        assert cov.closed_count == 200 and cov.missing_bars == 0


def test_api_down_marks_run_failed_and_writes_nothing(
    sqlite_sessions: sessionmaker[Session],
) -> None:
    r = backfill(
        sqlite_sessions,
        FakeInfo(InfoApiError("HTTP 503")),
        coin=COIN,
        interval=Interval.H1,
        clock=Clock(),
    )
    assert not r.ok and r.error and r.error.startswith("API_UNAVAILABLE")
    assert _count(sqlite_sessions) == 0
    with sqlite_sessions() as s:
        assert s.scalars(select(IngestionRun)).one().status == "FAILED"


def test_malformed_response_fails_closed(sqlite_sessions: sessionmaker[Session]) -> None:
    r = backfill(
        sqlite_sessions, FakeInfo({"error": "x"}), coin=COIN, interval=Interval.H1, clock=Clock()
    )
    assert not r.ok and "MALFORMED" in (r.error or "")


def test_bad_and_duplicate_bars_are_recorded_not_stored(
    sqlite_sessions: sessionmaker[Session], snapshot_1h: Any
) -> None:
    raw = [dict(b) for b in snapshot_1h]
    raw[20]["l"] = "-5"  # non-positive -> rejected
    raw[40]["h"] = raw[40]["l"]  # high < open/close -> invalid OHLC
    raw.append(dict(raw[60], v="1"))  # conflicting duplicate
    r = backfill(sqlite_sessions, FakeInfo(raw), coin=COIN, interval=Interval.H1, clock=Clock())
    assert r.ok and r.rejected == 2
    with sqlite_sessions() as s:
        types = sorted(i.issue_type for i in list_issues(s, COIN, Interval.H1))
        # The two rejected bars leave real gaps: they are reported, not filled.
        assert types == ["DUPLICATE_CONFLICT", "GAP", "GAP", "REJECTED_CANDLE", "REJECTED_CANDLE"]
        cov = coverage(s, COIN, Interval.H1)
        assert cov.missing_bars == 2


def test_sync_since_chunks_large_ranges(sqlite_sessions: sessionmaker[Session]) -> None:
    info = FakeInfo([])
    last = RECORDED_AT - Interval.M30.delta * (MAX_BARS_PER_REQUEST * 2 + 10)
    results = sync_since(
        sqlite_sessions,
        info,
        coin=COIN,
        interval=Interval.M30,
        last_closed_open_time=last,
        kind="gap_fill",
        clock=Clock(),
    )
    assert len(results) == 3 and all(r.ok for r in results)
    spans = [(c["req"]["endTime"] - c["req"]["startTime"]) // Interval.M30.ms for c in info.calls]
    assert max(spans) <= MAX_BARS_PER_REQUEST


def test_sync_stops_after_failed_chunk(sqlite_sessions: sessionmaker[Session]) -> None:
    info = FakeInfo(InfoApiError("down"))
    last = RECORDED_AT - Interval.M30.delta * (MAX_BARS_PER_REQUEST * 3)
    results = sync_since(
        sqlite_sessions,
        info,
        coin=COIN,
        interval=Interval.M30,
        last_closed_open_time=last,
        kind="gap_fill",
        clock=Clock(),
    )
    assert len(results) == 1 and not results[0].ok


def test_needs_finalize() -> None:
    iv = Interval.M30
    bar_1500 = RECORDED_AT.replace(minute=0)  # 15:00-15:30, closed at 15:45
    # Last closed = 15:00 bar; the next one (15:30) only closes at 16:00 -> nothing to do.
    assert not needs_finalize(bar_1500, iv, RECORDED_AT)
    # Last closed = 14:30 bar; the 15:00 bar has closed since -> finalize.
    assert needs_finalize(bar_1500 - iv.delta, iv, RECORDED_AT)
    assert needs_finalize(None, iv, RECORDED_AT)
    boundary = bar_1500 + 2 * iv.delta  # 16:00, end of the 15:30 bar
    assert not needs_finalize(bar_1500, iv, boundary)  # inside the grace period
    assert needs_finalize(bar_1500, iv, boundary + timedelta(seconds=5))
