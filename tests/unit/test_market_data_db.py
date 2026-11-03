"""Storage rules for candles (SQLite here; the same tests run on PostgreSQL in CI)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from autotrader.db.market_data import (
    closed_candles,
    coverage,
    forming_candle,
    list_issues,
    refresh_gaps,
    upsert_candles,
)
from autotrader.db.models import CandleRow
from autotrader.market.candles import Candle, Interval, parse_candle
from tests.conftest import RECORDED_AT

COIN = "xyz:CL"
IV = Interval.M30


def bars(raw: list[Any]) -> list[Candle]:
    out = [parse_candle(r, coin=COIN, interval=IV) for r in raw]
    return [c for c in out if isinstance(c, Candle)]


def count(s: Session) -> int:
    return int(s.scalar(select(func.count()).select_from(CandleRow)) or 0)


def test_rest_insert_marks_only_finished_bars_closed(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, bars(snapshot_30m), source="rest", now=RECORDED_AT)
    assert st.inserted == 201 and st.closed_now == 200
    with sqlite_sessions() as s:
        forming = forming_candle(s, COIN, IV)
        assert (
            forming is not None and forming.open_time.hour == 15 and forming.open_time.minute == 30
        )


def test_replay_is_idempotent(sqlite_sessions: sessionmaker[Session], snapshot_30m: Any) -> None:
    for _ in range(3):
        with sqlite_sessions.begin() as s:
            st = upsert_candles(s, bars(snapshot_30m), source="rest", now=RECORDED_AT)
    assert st.inserted == 0 and st.updated == 0 and st.unchanged == 201
    with sqlite_sessions() as s:
        assert count(s) == 201


def test_closed_bar_is_immutable_and_revision_is_reported(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars(snapshot_30m), source="rest", now=RECORDED_AT)
    revised = [dict(r) for r in snapshot_30m]
    revised[10]["v"] = str(Decimal(revised[10]["v"]) + 5)
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, bars(revised), source="rest", now=RECORDED_AT)
    assert st.updated == 0
    assert [f.issue_type for f in st.findings] == ["REVISED_CLOSED_CANDLE"]
    with sqlite_sessions() as s:
        row = s.scalar(select(CandleRow).where(CandleRow.volume == Decimal(snapshot_30m[10]["v"])))
        assert row is not None  # original value kept


def test_ws_never_closes_a_bar_and_late_ws_is_ignored(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    later = RECORDED_AT + timedelta(hours=2)
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, bars(snapshot_30m[-1:]), source="ws", now=later)
    assert st.inserted == 1 and st.closed_now == 0  # only REST closes bars
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars(snapshot_30m[-1:]), source="rest", now=later)
    stale = dict(snapshot_30m[-1], v="1", n=1, c=snapshot_30m[-1]["o"])
    stale["h"] = max(stale["o"], stale["h"], key=Decimal)
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, bars([stale]), source="ws", now=later)
    assert st.ignored_out_of_order == 1 and st.updated == 0


def test_out_of_order_ws_update_does_not_rewind_forming_bar(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    newest = snapshot_30m[-1]
    older = dict(newest, v=str(Decimal(newest["v"]) / 2), n=newest["n"] // 2)
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars([newest]), source="ws", now=RECORDED_AT)
    with sqlite_sessions.begin() as s:
        st = upsert_candles(s, bars([older]), source="ws", now=RECORDED_AT)
    assert st.ignored_out_of_order == 1
    with sqlite_sessions() as s:
        row = forming_candle(s, COIN, IV)
        assert row is not None and row.volume == Decimal(newest["v"])


def test_no_lookahead_closed_candles_as_of(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars(snapshot_30m), source="rest", now=RECORDED_AT)
    candles = bars(snapshot_30m)
    t = candles[100].close_time  # evaluating exactly at the close of bar #100
    with sqlite_sessions() as s:
        visible = closed_candles(s, COIN, IV, as_of=t)
        assert len(visible) == 101
        assert all(r.close_time.replace(tzinfo=None) <= t.replace(tzinfo=None) for r in visible)
        just_before = closed_candles(s, COIN, IV, as_of=t - timedelta(microseconds=1))
        assert len(just_before) == 100
        # The forming bar is never returned, whatever as_of is.
        far_future = closed_candles(s, COIN, IV, as_of=RECORDED_AT + timedelta(days=365))
        assert len(far_future) == 200
        assert len(closed_candles(s, COIN, IV, as_of=RECORDED_AT, limit=10)) == 10


def test_gaps_recorded_never_filled_and_resolved_when_data_arrives(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    holey = [r for i, r in enumerate(snapshot_30m) if i not in (50, 51, 52)]
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars(holey), source="rest", now=RECORDED_AT)
        gaps = refresh_gaps(s, COIN, IV, RECORDED_AT)
    assert [g.missing_bars for g in gaps] == [3]
    with sqlite_sessions() as s:
        assert count(s) == 198  # nothing synthesized
        cov = coverage(s, COIN, IV)
        assert cov.missing_bars == 3 and cov.open_gaps == 1
        assert [i.issue_type for i in list_issues(s, COIN, IV)] == ["GAP"]
    # Re-detection doesn't duplicate the issue.
    with sqlite_sessions.begin() as s:
        refresh_gaps(s, COIN, IV, RECORDED_AT)
    with sqlite_sessions() as s:
        issues = list_issues(s, COIN, IV)
        assert len(issues) == 1 and issues[0].occurrences == 2
    # The exchange later serves the missing bars: gap resolved, row kept for audit.
    with sqlite_sessions.begin() as s:
        upsert_candles(s, bars(snapshot_30m), source="rest", now=RECORDED_AT)
        assert refresh_gaps(s, COIN, IV, RECORDED_AT) == []
    with sqlite_sessions() as s:
        assert list_issues(s, COIN, IV) == []
        resolved = list_issues(s, COIN, IV, include_resolved=True)
        assert len(resolved) == 1 and resolved[0].resolved_at is not None


def test_mixed_coin_batch_rejected(
    sqlite_sessions: sessionmaker[Session], snapshot_30m: Any
) -> None:
    import pytest

    a = bars(snapshot_30m[:1])[0]
    other = Candle(
        **{**a.__dict__}
        if hasattr(a, "__dict__")
        else {
            f: getattr(a, f)
            for f in a.__slots__  # type: ignore[attr-defined]
        }
    )
    object.__setattr__(other, "coin", "xyz:GOLD")
    with pytest.raises(ValueError), sqlite_sessions.begin() as s:
        upsert_candles(s, [a, other], source="rest", now=RECORDED_AT)
