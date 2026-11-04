"""Runtime discovery of the HIP-3 market ``xyz:CL`` (read-only, fail-closed).

Flow:
1. ``perpDexs``                     -> confirm the builder dex (``xyz``) exists.
2. ``metaAndAssetCtxs`` with dex    -> locate the market *by exact name* and read
                                       precision, leverage, mark/oracle, funding.

Nothing is hard-coded: the asset index is looked up at runtime on every
discovery and is reported for information only. Any inconsistency raises
:class:`DiscoveryError` with a machine-readable ``code``; callers must treat that
as "market unavailable" and block all (paper) entries.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from autotrader.common.http import InfoApiError
from autotrader.common.safety import INFO_URL
from autotrader.market.models import (
    MAX_SIGNIFICANT_FIGURES,
    PERP_MAX_DECIMALS,
    AssetCtx,
    AssetMeta,
    MarketDiscovery,
    PerpDex,
)

DEFAULT_DEX = "xyz"
DEFAULT_COIN = "xyz:CL"
DEFAULT_MAX_DIVERGENCE_BPS = Decimal("50")

# Sanity bounds (fail closed outside them). Generous for any Hyperliquid perp,
# and well inside what the database columns can store without rounding/overflow.
PRICE_MIN = Decimal("1e-8")
PRICE_MAX = Decimal("1e9")
#: Hourly funding beyond +/-5% is treated as corrupt data (exchange caps are far lower).
FUNDING_ABS_MAX = Decimal("0.05")
OPEN_INTEREST_MAX = Decimal("1e15")
#: Mark/oracle divergence above 100% is treated as corrupt, not merely "degraded".
DIVERGENCE_HARD_MAX_BPS = Decimal("10000")


class DiscoveryErrorCode(StrEnum):
    API_UNAVAILABLE = "API_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    DEX_NOT_FOUND = "DEX_NOT_FOUND"
    MARKET_NOT_FOUND = "MARKET_NOT_FOUND"
    MARKET_AMBIGUOUS = "MARKET_AMBIGUOUS"
    MARKET_DELISTED = "MARKET_DELISTED"
    INVALID_PRICES = "INVALID_PRICES"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    DEX_AMBIGUOUS = "DEX_AMBIGUOUS"
    INVALID_TARGET = "INVALID_TARGET"


class DiscoveryError(RuntimeError):
    def __init__(self, code: DiscoveryErrorCode, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class InfoSource(Protocol):
    def info(self, payload: dict[str, Any]) -> Any: ...


def _sha256(*objs: Any) -> str:
    digest = hashlib.sha256()
    for obj in objs:
        digest.update(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _fetch(source: InfoSource, payload: dict[str, Any]) -> Any:
    try:
        return source.info(payload)
    except InfoApiError as exc:
        raise DiscoveryError(DiscoveryErrorCode.API_UNAVAILABLE, str(exc)) from exc


def divergence_bps(mark: Decimal, oracle: Decimal) -> Decimal:
    """|mark - oracle| / oracle in basis points."""
    return (abs(mark - oracle) / oracle * Decimal(10_000)).quantize(Decimal("0.01"))


def _check_ranges(ctx: AssetCtx) -> None:
    for label, value in (("markPx", ctx.mark_px), ("oraclePx", ctx.oracle_px)):
        if not PRICE_MIN <= value <= PRICE_MAX:
            raise DiscoveryError(
                DiscoveryErrorCode.OUT_OF_RANGE,
                f"{label}={value} outside sanity range [{PRICE_MIN}, {PRICE_MAX}]",
            )
    if abs(ctx.funding) > FUNDING_ABS_MAX:
        raise DiscoveryError(
            DiscoveryErrorCode.OUT_OF_RANGE, f"funding={ctx.funding} exceeds +/-{FUNDING_ABS_MAX}"
        )
    if not 0 <= ctx.open_interest <= OPEN_INTEREST_MAX:
        raise DiscoveryError(
            DiscoveryErrorCode.OUT_OF_RANGE, f"openInterest={ctx.open_interest} out of range"
        )


def discover_market(
    source: InfoSource,
    *,
    dex: str = DEFAULT_DEX,
    coin: str = DEFAULT_COIN,
    max_divergence_bps: Decimal = DEFAULT_MAX_DIVERGENCE_BPS,
    now: datetime | None = None,
) -> MarketDiscovery:
    """Discover and validate ``coin`` on builder dex ``dex``. Fail closed on any doubt."""
    prefix, sep, symbol = coin.partition(":")
    if not dex or not sep or prefix != dex or not symbol:
        # Guards against accidentally using the native "CL" (or another dex's market).
        raise DiscoveryError(
            DiscoveryErrorCode.INVALID_TARGET,
            f"Coin {coin!r} must be namespaced by dex {dex!r} (e.g. 'xyz:CL').",
        )

    # 1) perpDexs
    dexs_raw = _fetch(source, {"type": "perpDexs"})
    if not isinstance(dexs_raw, list):
        raise DiscoveryError(DiscoveryErrorCode.MALFORMED_RESPONSE, "perpDexs is not a list")
    # index 0 is the native (validator-operated) dex and is reported as null
    dex_matches = [
        i for i, e in enumerate(dexs_raw) if isinstance(e, dict) and e.get("name") == dex
    ]
    if not dex_matches:
        raise DiscoveryError(DiscoveryErrorCode.DEX_NOT_FOUND, f"dex {dex!r} not in perpDexs")
    if len(dex_matches) > 1:
        raise DiscoveryError(
            DiscoveryErrorCode.DEX_AMBIGUOUS, f"dex {dex!r} appears {len(dex_matches)} times"
        )
    dex_index = dex_matches[0]
    try:
        perp_dex = PerpDex.model_validate(dexs_raw[dex_index])
    except ValidationError as exc:
        raise DiscoveryError(DiscoveryErrorCode.MALFORMED_RESPONSE, str(exc)) from exc

    # 2) metaAndAssetCtxs for the dex
    meta_raw = _fetch(source, {"type": "metaAndAssetCtxs", "dex": dex})
    if (
        not isinstance(meta_raw, list)
        or len(meta_raw) != 2
        or not isinstance(meta_raw[0], dict)
        or not isinstance(meta_raw[1], list)
        or not isinstance(meta_raw[0].get("universe"), list)
    ):
        raise DiscoveryError(
            DiscoveryErrorCode.MALFORMED_RESPONSE, "metaAndAssetCtxs has unexpected shape"
        )
    universe: list[Any] = meta_raw[0]["universe"]
    ctxs: list[Any] = meta_raw[1]
    if len(universe) != len(ctxs):
        raise DiscoveryError(
            DiscoveryErrorCode.MALFORMED_RESPONSE,
            f"universe ({len(universe)}) and assetCtxs ({len(ctxs)}) length mismatch",
        )

    matches = [i for i, a in enumerate(universe) if isinstance(a, dict) and a.get("name") == coin]
    if not matches:
        raise DiscoveryError(
            DiscoveryErrorCode.MARKET_NOT_FOUND, f"{coin!r} not found in dex {dex!r}"
        )
    if len(matches) > 1:
        raise DiscoveryError(
            DiscoveryErrorCode.MARKET_AMBIGUOUS, f"{coin!r} appears {len(matches)} times"
        )
    asset_index = matches[0]

    try:
        meta = AssetMeta.model_validate(universe[asset_index])
        ctx = AssetCtx.model_validate(ctxs[asset_index])
    except ValidationError as exc:
        raise DiscoveryError(DiscoveryErrorCode.MALFORMED_RESPONSE, str(exc)) from exc

    if meta.is_delisted:
        raise DiscoveryError(DiscoveryErrorCode.MARKET_DELISTED, f"{coin!r} is delisted")
    if ctx.mark_px <= 0 or ctx.oracle_px <= 0:
        raise DiscoveryError(
            DiscoveryErrorCode.INVALID_PRICES,
            f"non-positive prices: mark={ctx.mark_px} oracle={ctx.oracle_px}",
        )

    _check_ranges(ctx)
    try:
        div = divergence_bps(ctx.mark_px, ctx.oracle_px)
    except ArithmeticError as exc:  # defensive: bounds above should make this impossible
        raise DiscoveryError(DiscoveryErrorCode.OUT_OF_RANGE, f"divergence: {exc!r}") from exc
    if div > DIVERGENCE_HARD_MAX_BPS:
        raise DiscoveryError(
            DiscoveryErrorCode.OUT_OF_RANGE,
            f"mark/oracle divergence {div} bps is implausible (> {DIVERGENCE_HARD_MAX_BPS})",
        )
    warnings: list[str] = []
    if div > max_divergence_bps:
        warnings.append(f"ORACLE_MARK_DIVERGENCE: {div} bps exceeds limit {max_divergence_bps} bps")
    if ctx.mid_px is None:
        warnings.append("NO_MID_PRICE: order book may be empty")
    elif not PRICE_MIN <= ctx.mid_px <= PRICE_MAX:
        warnings.append(f"INVALID_MID_PRICE: {ctx.mid_px}")
    if ctx.open_interest == 0:
        warnings.append("ZERO_OPEN_INTEREST")

    return MarketDiscovery(
        dex=dex,
        dex_full_name=perp_dex.full_name,
        dex_index=dex_index,
        coin=coin,
        asset_index_in_dex=asset_index,
        sz_decimals=meta.sz_decimals,
        max_price_decimals=PERP_MAX_DECIMALS - meta.sz_decimals,
        max_significant_figures=MAX_SIGNIFICANT_FIGURES,
        max_leverage=meta.max_leverage,
        only_isolated=meta.only_isolated,
        margin_mode=meta.margin_mode,
        growth_mode=meta.growth_mode,
        deployer_fee_scale=meta.deployer_fee_scale,
        mark_px=ctx.mark_px,
        oracle_px=ctx.oracle_px,
        mid_px=ctx.mid_px,
        funding_rate=ctx.funding,
        open_interest=ctx.open_interest,
        day_notional_volume=ctx.day_ntl_vlm,
        oracle_mark_divergence_bps=div,
        status="DEGRADED" if warnings else "ACTIVE",
        warnings=warnings,
        discovered_at=now or datetime.now(tz=UTC),
        source=INFO_URL,
        raw_sha256=_sha256(dexs_raw, meta_raw),
    )
