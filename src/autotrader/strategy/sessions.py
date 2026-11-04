"""External reference session for WTI (NYMEX CL on CME Globex) - entry gating.

``xyz:CL`` is a HIP-3 perpetual that keeps trading 24/7, but its price
reference (WTI crude futures) does not. Outside the reference session the perp
trades on thin, internally discovered prices: in the recorded 1h data the
median weekend volume is roughly 10x lower than on weekdays, and the volume
spike on Sunday 22:00 UTC (= 18:00 New York, summer time) matches the Globex
reopen. New entries are therefore allowed only while the reference session is
open, away from its edges, and never on a known holiday or near contract roll.

Schedule (America/New_York, DST handled by ``zoneinfo``):

* open Sunday 18:00 -> Friday 17:00
* daily maintenance break 17:00 -> 18:00, Monday-Thursday

Calendar coverage is explicit: outside ``CALENDAR_START..CALENDAR_END`` the
schedule is unknown and :meth:`ReferenceSession.entry_block_reason` fails closed.

Provenance: the weekly schedule and the holiday list are from prior knowledge
of the CME Globex energy calendar. The official CME page could not be reached
from the development environment, so they are labelled UNVERIFIED and
treated conservatively: holidays with only an early halt (MLK, Presidents',
Memorial, Juneteenth, July 4th, Labor, Thanksgiving) block entries for the
whole New York calendar day, as do the early-close days around Thanksgiving
and Christmas.

Contract roll: the CL front month stops trading 3 business days before the
25th calendar day of the month before delivery (4 if the 25th is not a
business day). How and when the ``xyz:CL`` oracle rolls is not documented in
anything we could verify, so entries are blocked on the last ``roll_blackout``
business days up to and including each expiry (UNVERIFIED assumption).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

NY: Final = ZoneInfo("America/New_York")
CALENDAR_START: Final = date(2025, 1, 1)
CALENDAR_END: Final = date(2027, 12, 31)
PROVENANCE: Final = "UNVERIFIED: CME Globex energy schedule from prior knowledge"

#: New York calendar days on which new entries are blocked (closed or shortened).
HOLIDAYS: Final[frozenset[date]] = frozenset(
    {
        # 2025
        date(2025, 1, 1),
        date(2025, 1, 9),  # national day of mourning (CME early close)
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 3),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 11, 28),
        date(2025, 12, 24),
        date(2025, 12, 25),
        date(2025, 12, 31),
        # 2026
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 11, 27),
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 31),
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 11, 26),
        date(2027, 12, 23),
        date(2027, 12, 24),
        date(2027, 12, 31),
    }
)

_DAILY_CLOSE: Final = time(17, 0)
_DAILY_OPEN: Final = time(18, 0)


def _is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def _shift_business_days(d: date, n: int) -> date:
    step = -1 if n < 0 else 1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if _is_business_day(d):
            remaining -= 1
    return d


@lru_cache(maxsize=64)
def cl_expiry(year: int, month: int) -> date:
    """Last trading day of the CL contract that expires in ``year-month``."""
    d25 = date(year, month, 25)
    back = 3 if _is_business_day(d25) else 4
    # count business days strictly before the 25th
    d = d25
    while back:
        d -= timedelta(days=1)
        if _is_business_day(d):
            back -= 1
    return d


@dataclass(frozen=True, slots=True)
class ReferenceSession:
    """Entry gate derived from the external WTI reference session."""

    #: no new entries within this many minutes after a (re)open or before a close
    edge_buffer: timedelta = timedelta(minutes=30)
    #: business days (ending at expiry) blocked around the CL roll; 0 disables
    roll_blackout: int = 2

    @staticmethod
    def is_open(at: datetime) -> bool:
        """True while CME Globex CL trades (holidays NOT considered here)."""
        local = at.astimezone(NY)
        wd, t = local.weekday(), local.time()
        if wd == 5:  # Saturday
            return False
        if wd == 6:  # Sunday: opens 18:00
            return t >= _DAILY_OPEN
        if wd == 4:  # Friday: closes 17:00 for the weekend
            return t < _DAILY_CLOSE
        return not (_DAILY_CLOSE <= t < _DAILY_OPEN)

    def entry_block_reason(self, at: datetime) -> str | None:
        """``None`` if a new entry may be placed at ``at``; otherwise a reason code."""
        if at.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        local = at.astimezone(NY)
        day = local.date()
        if not CALENDAR_START <= day <= CALENDAR_END:
            return "SESSION_CALENDAR_UNKNOWN"
        if day in HOLIDAYS:
            return "REFERENCE_HOLIDAY"
        if not self.is_open(at):
            return "REFERENCE_SESSION_CLOSED"
        if not (self.is_open(at - self.edge_buffer) and self.is_open(at + self.edge_buffer)):
            return "REFERENCE_SESSION_EDGE"
        if self.roll_blackout > 0 and self._in_roll_window(day):
            return "CONTRACT_ROLL_WINDOW"
        return None

    def _in_roll_window(self, day: date) -> bool:
        for y, m in ((day.year, day.month), _next_month(day.year, day.month)):
            expiry = cl_expiry(y, m)
            start = _shift_business_days(expiry, -(self.roll_blackout - 1))
            if start <= day <= expiry:
                return True
        return False


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def to_utc(local: datetime) -> datetime:
    return local.astimezone(UTC)
