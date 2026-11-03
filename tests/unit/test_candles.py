"""Candle parsing, per-bar validation and series quality checks (pure logic)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from autotrader.market.candles import (
    CLOSE_GRACE,
    Candle,
    CandleRejection,
    Interval,
    Rejected,
    parse_candle,
)
from autotrader.market.quality import IssueType, dedupe, find_gaps, find_outliers

COIN = "xyz:CL"


def _parse_all(raw: list[Any], interval: Interval) -> list[Candle]:
    out = [parse_candle(r, coin=COIN, interval=interval) for r in raw]
    assert all(isinstance(c, Candle) for c in out), [c for c in out if isinstance(c, Rejected)]
    return out  # type: ignore[return-value]


def test_real_snapshots_parse_cleanly(snapshot_30m: Any, snapshot_1h: Any) -> None:
    for raw, iv in ((snapshot_30m, Interval.M30), (snapshot_1h, Interval.H1)):
        candles = _parse_all(raw, iv)
        assert len(candles) == 201
        assert find_gaps([c.open_time for c in candles], iv) == []
        assert all(c.close_time - c.open_time == iv.delta for c in candles)


def test_decimal_exactness(snapshot_30m: Any) -> None:
    c = parse_candle(snapshot_30m[0], coin=COIN, interval=Interval.M30)
    assert isinstance(c, Candle)
    assert isinstance(c.close, Decimal) and str(c.close) == snapshot_30m[0]["c"]


def _bar(**over: Any) -> dict[str, Any]:
    bar = {
        "t": 1793719800000,
        "T": 1793721599999,
        "s": COIN,
        "i": "30m",
        "o": "96.37",
        "c": "96.18",
        "h": "96.56",
        "l": "96.11",
        "v": "3546.51",
        "n": 303,
    }
    bar.update(over)
    return bar


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"o": "0"}, CandleRejection.NON_POSITIVE_PRICE),
        ({"l": "-1"}, CandleRejection.NON_POSITIVE_PRICE),
        ({"h": "96.0"}, CandleRejection.INVALID_OHLC),  # high below open
        ({"l": "96.5"}, CandleRejection.INVALID_OHLC),  # low above close
        ({"v": "-0.1"}, CandleRejection.NEGATIVE_VOLUME),
        ({"c": "1e12", "h": "1e12"}, CandleRejection.OUT_OF_RANGE),
        ({"c": "NaN"}, CandleRejection.MALFORMED),
        ({"c": "Infinity"}, CandleRejection.MALFORMED),
        ({"c": 96.18}, CandleRejection.MALFORMED),  # floats are not accepted
        ({"c": True}, CandleRejection.MALFORMED),
        ({"t": "1793719800000"}, CandleRejection.MALFORMED),
        ({"t": 1793719800001, "T": 1793721600000}, CandleRejection.MISALIGNED_TIME),
        ({"T": 1793721600000}, CandleRejection.MISALIGNED_TIME),
        ({"s": "CL"}, CandleRejection.WRONG_MARKET),  # native CL, not xyz:CL
        ({"i": "1h"}, CandleRejection.WRONG_MARKET),
    ],
)
def test_invalid_bars_are_rejected_not_fixed(over: dict[str, Any], reason: CandleRejection) -> None:
    r = parse_candle(_bar(**over), coin=COIN, interval=Interval.M30)
    assert isinstance(r, Rejected)
    assert r.reason == reason


@pytest.mark.parametrize("raw", [None, [], "x", {"t": 1}])
def test_malformed_payloads(raw: Any) -> None:
    r = parse_candle(raw, coin=COIN, interval=Interval.M30)
    assert isinstance(r, Rejected) and r.reason == CandleRejection.MALFORMED


def test_closed_only_after_end_plus_grace() -> None:
    c = parse_candle(_bar(), coin=COIN, interval=Interval.M30)
    assert isinstance(c, Candle)
    end = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)
    assert c.close_time == end
    assert not c.is_closed_at(end - timedelta(seconds=1))
    assert not c.is_closed_at(end)  # the exchange may still be updating it
    assert not c.is_closed_at(end + CLOSE_GRACE - timedelta(milliseconds=1))
    assert c.is_closed_at(end + CLOSE_GRACE)


def test_dedupe_identical_and_conflicting(snapshot_30m: Any) -> None:
    candles = _parse_all(snapshot_30m[:5], Interval.M30)
    altered = dict(snapshot_30m[2], v=str(Decimal(snapshot_30m[2]["v"]) + 1))
    conflict = parse_candle(altered, coin=COIN, interval=Interval.M30)
    assert isinstance(conflict, Candle)
    unique, findings = dedupe([*candles, candles[1], conflict])
    assert len(unique) == 5
    assert [f.issue_type for f in findings] == [IssueType.DUPLICATE_CONFLICT]
    assert unique[2] == conflict  # last received wins, but the conflict is reported


def test_gaps_are_detected_and_counted(snapshot_30m: Any) -> None:
    candles = _parse_all(snapshot_30m[:20], Interval.M30)
    times = [c.open_time for i, c in enumerate(candles) if i not in (5, 6, 7, 12)]
    gaps = find_gaps(times, Interval.M30)
    assert [(g.first_missing, g.missing_bars) for g in gaps] == [
        (candles[5].open_time, 3),
        (candles[12].open_time, 1),
    ]
    assert gaps[0].resumes_at == candles[8].open_time


def test_outliers_flagged_only_beyond_thresholds(snapshot_1h: Any) -> None:
    candles = _parse_all(snapshot_1h, Interval.H1)
    assert find_outliers(candles, Interval.H1) == []  # real data: no outliers
    raw = copy.deepcopy(snapshot_1h[:3])
    raw[2]["c"] = str(Decimal(raw[1]["c"]) * Decimal("1.2"))
    raw[2]["h"] = raw[2]["c"]
    spiky = _parse_all(raw, Interval.H1)
    types = {f.issue_type for f in find_outliers(spiky, Interval.H1)}
    assert IssueType.OUTLIER_RETURN in types
    assert IssueType.OUTLIER_RANGE in types
