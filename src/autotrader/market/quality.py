"""Series-level data-quality checks (pure logic, no I/O).

Nothing here modifies prices: missing bars are reported as gaps, never filled;
outliers are flagged, never smoothed. Callers persist the findings.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final

from autotrader.market.candles import Candle, Interval


class IssueType(StrEnum):
    GAP = "GAP"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    REJECTED_CANDLE = "REJECTED_CANDLE"
    OUTLIER_RETURN = "OUTLIER_RETURN"
    OUTLIER_RANGE = "OUTLIER_RANGE"
    REVISED_CLOSED_CANDLE = "REVISED_CLOSED_CANDLE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    STALE_DATA = "STALE_DATA"


#: Flag (not reject) a bar whose close-to-close move or high-low range exceeds
#: these fractions. Chosen above the largest moves observed in the available
#: xyz:CL history (2026-11-03: 3.7% per 30m bar, 6.0% per 1h bar).
OUTLIER_RETURN: Final[dict[Interval, Decimal]] = {
    Interval.M30: Decimal("0.08"),
    Interval.H1: Decimal("0.10"),
}
OUTLIER_RANGE: Final[dict[Interval, Decimal]] = {
    Interval.M30: Decimal("0.10"),
    Interval.H1: Decimal("0.12"),
}


@dataclass(frozen=True, slots=True)
class Gap:
    #: First missing bar open time and the open time of the bar after the gap.
    first_missing: datetime
    resumes_at: datetime
    missing_bars: int


@dataclass(frozen=True, slots=True)
class Finding:
    issue_type: IssueType
    open_time: datetime
    detail: str


def dedupe(candles: Iterable[Candle]) -> tuple[list[Candle], list[Finding]]:
    """Collapse identical duplicates; report conflicting duplicates.

    For a conflicting pair the *last* one received wins (exchange data for an
    open bar evolves), and the conflict is reported so it is never hidden.
    """
    by_time: dict[datetime, Candle] = {}
    findings: list[Finding] = []
    for c in candles:
        prev = by_time.get(c.open_time)
        if prev is not None and not prev.same_values(c):
            findings.append(
                Finding(
                    IssueType.DUPLICATE_CONFLICT,
                    c.open_time,
                    f"conflicting values for same bar: close {prev.close} vs {c.close}, "
                    f"volume {prev.volume} vs {c.volume}",
                )
            )
        by_time[c.open_time] = c
    return [by_time[t] for t in sorted(by_time)], findings


def find_gaps(open_times: Sequence[datetime], interval: Interval) -> list[Gap]:
    """Missing bars between consecutive (sorted, unique) open times."""
    gaps: list[Gap] = []
    step = interval.delta
    for prev, nxt in pairwise(open_times):
        diff = nxt - prev
        if diff > step:
            missing = int(diff / step) - 1
            gaps.append(Gap(prev + step, nxt, missing))
    return gaps


def find_outliers(candles: Sequence[Candle], interval: Interval) -> list[Finding]:
    """Flag implausible moves on a sorted series of bars."""
    findings: list[Finding] = []
    ret_max, range_max = OUTLIER_RETURN[interval], OUTLIER_RANGE[interval]
    prev: Candle | None = None
    for c in candles:
        rng = (c.high - c.low) / c.close
        if rng > range_max:
            findings.append(
                Finding(IssueType.OUTLIER_RANGE, c.open_time, f"high-low range {rng:.4%}")
            )
        if prev is not None and c.open_time - prev.open_time == interval.delta:
            ret = abs(c.close / prev.close - 1)
            if ret > ret_max:
                findings.append(
                    Finding(IssueType.OUTLIER_RETURN, c.open_time, f"close-to-close {ret:.4%}")
                )
        prev = c
    return findings
