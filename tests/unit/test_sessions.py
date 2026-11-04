"""WTI reference-session gate (America/New_York, DST, holidays, roll, fail-closed)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from autotrader.strategy.sessions import ReferenceSession, cl_expiry

S = ReferenceSession()


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def test_weekend_closed_and_sunday_reopen_with_edge_buffer() -> None:
    # Sat 2026-10-17 15:00 UTC -> closed
    assert S.entry_block_reason(utc(2026, 10, 17, 15)) == "REFERENCE_SESSION_CLOSED"
    # Sun 2026-10-18 21:59 UTC = 17:59 EDT -> closed; 22:00 = 18:00 opens
    assert not S.is_open(utc(2026, 10, 18, 21, 59))
    assert S.is_open(utc(2026, 10, 18, 22, 0))
    # inside the 30-minute reopen buffer: still no entries
    assert S.entry_block_reason(utc(2026, 10, 18, 22, 15)) == "REFERENCE_SESSION_EDGE"
    assert S.entry_block_reason(utc(2026, 10, 18, 22, 30)) is None


def test_friday_close_and_daily_break() -> None:
    assert S.is_open(utc(2026, 10, 16, 20, 59))  # Fri 16:59 EDT
    assert not S.is_open(utc(2026, 10, 16, 21, 0))  # Fri 17:00 EDT
    assert S.entry_block_reason(utc(2026, 10, 16, 20, 45)) == "REFERENCE_SESSION_EDGE"
    # Tuesday 17:00-18:00 EDT maintenance break
    assert not S.is_open(utc(2026, 10, 13, 21, 30))
    assert S.entry_block_reason(utc(2026, 10, 13, 21, 30)) == "REFERENCE_SESSION_CLOSED"
    assert S.entry_block_reason(utc(2026, 10, 13, 15, 0)) is None


def test_dst_shifts_the_utc_schedule() -> None:
    # 22:00 UTC on a Tuesday: 18:00 EDT in October (open), 17:00 EST in December (break)
    assert S.is_open(utc(2026, 10, 13, 22, 0))
    assert not S.is_open(utc(2026, 12, 8, 22, 0))
    assert S.is_open(utc(2026, 12, 8, 23, 0))


def test_holidays_block_entries_all_day() -> None:
    assert S.entry_block_reason(utc(2026, 4, 3, 15)) == "REFERENCE_HOLIDAY"  # Good Friday
    assert S.entry_block_reason(utc(2026, 11, 26, 15)) == "REFERENCE_HOLIDAY"  # Thanksgiving
    assert S.entry_block_reason(utc(2026, 12, 25, 15)) == "REFERENCE_HOLIDAY"


def test_unknown_calendar_fails_closed() -> None:
    assert S.entry_block_reason(utc(2028, 3, 7, 15)) == "SESSION_CALENDAR_UNKNOWN"
    assert S.entry_block_reason(utc(2024, 3, 5, 15)) == "SESSION_CALENDAR_UNKNOWN"
    with pytest.raises(ValueError):
        S.entry_block_reason(datetime(2026, 10, 13, 15))  # naive


def test_cl_expiry_rule() -> None:
    # 25th is a business day -> 3 business days before
    assert cl_expiry(2025, 11) == date(2025, 11, 20)
    assert cl_expiry(2026, 11) == date(2026, 11, 20)
    # 25th on a Sunday -> 4 business days before
    assert cl_expiry(2026, 1) == date(2026, 1, 20)
    # Good Friday is not a business day (2026-04-03 is before the 25th; rule still holds)
    assert cl_expiry(2026, 4) == date(2026, 4, 21)


def test_roll_window_blocks_entries() -> None:
    assert S.entry_block_reason(utc(2026, 11, 20, 15)) == "CONTRACT_ROLL_WINDOW"
    assert S.entry_block_reason(utc(2026, 11, 19, 15)) == "CONTRACT_ROLL_WINDOW"
    assert S.entry_block_reason(utc(2026, 11, 18, 15)) is None
    assert ReferenceSession(roll_blackout=0).entry_block_reason(utc(2026, 11, 20, 15)) is None
