"""Typed views of Hyperliquid Info API responses (public, read-only).

Numeric strings are parsed into ``Decimal``; floats are never used for prices.
Unknown fields are ignored so new exchange fields don't break discovery, but
fields we rely on are required and validated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Hyperliquid perps: price decimals <= MAX_DECIMALS - szDecimals and at most
#: 5 significant figures (documented "Tick and lot size" rule).
PERP_MAX_DECIMALS = 6
MAX_SIGNIFICANT_FIGURES = 5


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class PerpDex(_Lenient):
    name: str
    full_name: str | None = Field(default=None, alias="fullName")
    deployer: str | None = None


class AssetMeta(_Lenient):
    name: str
    # strict: reject bools/strings ("20", true) instead of silently coercing them
    sz_decimals: int = Field(alias="szDecimals", ge=0, le=PERP_MAX_DECIMALS, strict=True)
    max_leverage: int = Field(alias="maxLeverage", gt=0, le=1000, strict=True)
    margin_table_id: int | None = Field(default=None, alias="marginTableId")
    only_isolated: bool = Field(default=False, alias="onlyIsolated", strict=True)
    margin_mode: str | None = Field(default=None, alias="marginMode")
    is_delisted: bool = Field(default=False, alias="isDelisted", strict=True)
    growth_mode: str | None = Field(default=None, alias="growthMode")
    deployer_fee_scale: Decimal | None = Field(default=None, alias="deployerFeeScale")


class AssetCtx(_Lenient):
    funding: Decimal
    open_interest: Decimal = Field(alias="openInterest")
    prev_day_px: Decimal | None = Field(default=None, alias="prevDayPx")
    day_ntl_vlm: Decimal | None = Field(default=None, alias="dayNtlVlm")
    premium: Decimal | None = None
    oracle_px: Decimal = Field(alias="oraclePx")
    mark_px: Decimal = Field(alias="markPx")
    mid_px: Decimal | None = Field(default=None, alias="midPx")
    impact_pxs: list[Decimal] | None = Field(default=None, alias="impactPxs")


MarketStatus = Literal["ACTIVE", "DEGRADED"]


class MarketDiscovery(BaseModel):
    """Result of a successful runtime discovery of the target market."""

    model_config = ConfigDict(frozen=True)

    dex: str
    dex_full_name: str | None
    dex_index: int = Field(description="Position of the dex in perpDexs (0 = native)")
    coin: str
    asset_index_in_dex: int = Field(
        description="Index in the dex universe at discovery time. Informational only; "
        "never cached or used as an order asset id."
    )
    sz_decimals: int
    max_price_decimals: int
    max_significant_figures: int
    max_leverage: int
    only_isolated: bool
    margin_mode: str | None
    growth_mode: str | None
    deployer_fee_scale: Decimal | None
    mark_px: Decimal
    oracle_px: Decimal
    mid_px: Decimal | None
    funding_rate: Decimal
    open_interest: Decimal
    day_notional_volume: Decimal | None
    oracle_mark_divergence_bps: Decimal
    status: MarketStatus
    warnings: list[str]
    discovered_at: datetime
    source: str
    raw_sha256: str
