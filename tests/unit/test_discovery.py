"""Market discovery against recorded mainnet responses (2026-11-03) and edge cases."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from autotrader.common.http import InfoApiError
from autotrader.market.discovery import (
    DiscoveryError,
    DiscoveryErrorCode,
    discover_market,
    divergence_bps,
)
from tests.conftest import FakeInfo


def _cl_index(meta: Any) -> int:
    return next(i for i, a in enumerate(meta[0]["universe"]) if a["name"] == "xyz:CL")


def test_discovers_xyz_cl_from_recorded_mainnet(fake_info: FakeInfo) -> None:
    d = discover_market(fake_info)
    assert d.coin == "xyz:CL"
    assert d.dex == "xyz"
    assert d.dex_index == 1
    assert d.sz_decimals == 3
    assert d.max_price_decimals == 3
    assert d.max_leverage == 20
    assert d.mark_px == Decimal("94.913")
    assert d.oracle_px == Decimal("94.9")
    assert d.status == "ACTIVE"
    assert d.warnings == []
    assert len(d.raw_sha256) == 64
    assert fake_info.calls == [
        {"type": "perpDexs"},
        {"type": "metaAndAssetCtxs", "dex": "xyz"},
    ]


def test_asset_index_is_looked_up_not_hardcoded(fake_info: FakeInfo, meta_xyz: Any) -> None:
    # Move xyz:CL to the end of the universe: discovery must follow it.
    meta = copy.deepcopy(meta_xyz)
    i = _cl_index(meta)
    meta[0]["universe"].append(meta[0]["universe"].pop(i))
    meta[1].append(meta[1].pop(i))
    fake_info.responses["metaAndAssetCtxs"] = meta
    d = discover_market(fake_info)
    assert d.asset_index_in_dex == len(meta[0]["universe"]) - 1
    assert d.mark_px == Decimal("94.913")


@pytest.mark.parametrize("coin", ["CL", "flx:CL", "xyz:", ":CL"])
def test_rejects_non_namespaced_targets(fake_info: FakeInfo, coin: str) -> None:
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info, coin=coin)
    assert e.value.code == DiscoveryErrorCode.INVALID_TARGET
    assert fake_info.calls == []


def test_dex_missing(fake_info: FakeInfo) -> None:
    fake_info.responses["perpDexs"] = [None, {"name": "flx"}]
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.DEX_NOT_FOUND


def test_market_missing(fake_info: FakeInfo, meta_xyz: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    i = _cl_index(meta)
    del meta[0]["universe"][i]
    del meta[1][i]
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MARKET_NOT_FOUND


def test_market_duplicated_is_ambiguous(fake_info: FakeInfo, meta_xyz: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    i = _cl_index(meta)
    meta[0]["universe"].append(meta[0]["universe"][i])
    meta[1].append(meta[1][i])
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MARKET_AMBIGUOUS


def test_delisted_market_fails_closed(fake_info: FakeInfo, meta_xyz: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    meta[0]["universe"][_cl_index(meta)]["isDelisted"] = True
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MARKET_DELISTED


@pytest.mark.parametrize("field,value", [("markPx", "0"), ("oraclePx", "-1")])
def test_invalid_prices_fail_closed(
    fake_info: FakeInfo, meta_xyz: Any, field: str, value: str
) -> None:
    meta = copy.deepcopy(meta_xyz)
    meta[1][_cl_index(meta)][field] = value
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.INVALID_PRICES


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__(1, m[1][:-1]),  # length mismatch
        lambda m: m.pop(),  # wrong shape
        lambda m: m[1].__setitem__(_cl_index(m), {"markPx": "1"}),  # missing ctx fields
        lambda m: m[1][_cl_index(m)].__setitem__("markPx", "not-a-number"),
        lambda m: m[0]["universe"][_cl_index(m)].pop("szDecimals"),
    ],
)
def test_malformed_responses_fail_closed(fake_info: FakeInfo, meta_xyz: Any, mutate: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    mutate(meta)
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MALFORMED_RESPONSE


def test_perp_dexs_not_a_list(fake_info: FakeInfo) -> None:
    fake_info.responses["perpDexs"] = {"error": "x"}
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MALFORMED_RESPONSE


def test_api_unavailable(fake_info: FakeInfo) -> None:
    fake_info.responses["perpDexs"] = InfoApiError("HTTP 429")
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.API_UNAVAILABLE


def test_divergence_and_missing_mid_degrade(fake_info: FakeInfo, meta_xyz: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    ctx = meta[1][_cl_index(meta)]
    ctx["markPx"] = "96.0"
    ctx["oraclePx"] = "94.9"
    ctx["midPx"] = None
    fake_info.responses["metaAndAssetCtxs"] = meta
    d = discover_market(fake_info, max_divergence_bps=Decimal("50"))
    assert d.status == "DEGRADED"
    assert any(w.startswith("ORACLE_MARK_DIVERGENCE") for w in d.warnings)
    assert any(w.startswith("NO_MID_PRICE") for w in d.warnings)


def test_divergence_bps_math() -> None:
    assert divergence_bps(Decimal("101"), Decimal("100")) == Decimal("100.00")
    assert divergence_bps(Decimal("99"), Decimal("100")) == Decimal("100.00")
    assert divergence_bps(Decimal("94.913"), Decimal("94.9")) == Decimal("1.37")


# ---------------------------------------------------------------- review regressions (M1, L2)


def _with_ctx(fake_info: FakeInfo, meta_xyz: Any, **fields: Any) -> None:
    meta = copy.deepcopy(meta_xyz)
    meta[1][_cl_index(meta)].update(fields)
    fake_info.responses["metaAndAssetCtxs"] = meta


@pytest.mark.parametrize(
    "fields",
    [
        {"markPx": "1e40", "oraclePx": "1e-30"},  # used to raise decimal.InvalidOperation
        {"markPx": "1e25", "oraclePx": "1e25"},  # used to overflow NUMERIC(38,18) on insert
        {"markPx": "1e-12", "oraclePx": "1e-12"},  # below storable precision
        {"funding": "0.5"},
        {"funding": "-0.06"},
        {"openInterest": "-1"},
        {"openInterest": "1e20"},
        {"markPx": "300", "oraclePx": "94.9"},  # > 100% divergence: corrupt, not "degraded"
    ],
)
def test_out_of_range_values_fail_closed(
    fake_info: FakeInfo, meta_xyz: Any, fields: dict[str, str]
) -> None:
    _with_ctx(fake_info, meta_xyz, **fields)
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.OUT_OF_RANGE


@pytest.mark.parametrize("mid", ["0", "-1"])
def test_non_positive_mid_degrades(fake_info: FakeInfo, meta_xyz: Any, mid: str) -> None:
    _with_ctx(fake_info, meta_xyz, midPx=mid)
    d = discover_market(fake_info)
    assert d.status == "DEGRADED"
    assert any(w.startswith("INVALID_MID_PRICE") for w in d.warnings)


def test_zero_open_interest_degrades(fake_info: FakeInfo, meta_xyz: Any) -> None:
    _with_ctx(fake_info, meta_xyz, openInterest="0")
    assert discover_market(fake_info).warnings == ["ZERO_OPEN_INTEREST"]


def test_duplicate_dex_is_ambiguous(fake_info: FakeInfo, perp_dexs: Any) -> None:
    fake_info.responses["perpDexs"] = [*perp_dexs, {"name": "xyz", "fullName": "Impostor"}]
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.DEX_AMBIGUOUS


@pytest.mark.parametrize(
    "field,value",
    [("szDecimals", True), ("szDecimals", "3"), ("maxLeverage", "20"), ("isDelisted", "false")],
)
def test_no_silent_type_coercion(
    fake_info: FakeInfo, meta_xyz: Any, field: str, value: object
) -> None:
    meta = copy.deepcopy(meta_xyz)
    meta[0]["universe"][_cl_index(meta)][field] = value
    fake_info.responses["metaAndAssetCtxs"] = meta
    with pytest.raises(DiscoveryError) as e:
        discover_market(fake_info)
    assert e.value.code == DiscoveryErrorCode.MALFORMED_RESPONSE


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True])
def test_non_finite_prices_rejected(fake_info: FakeInfo, meta_xyz: Any, value: object) -> None:
    _with_ctx(fake_info, meta_xyz, markPx=value)
    with pytest.raises(DiscoveryError):
        discover_market(fake_info)
