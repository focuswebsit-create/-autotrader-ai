"""Transaction-cost and funding model, with every input labelled by provenance.

* ``VERIFIED``   - taken from exchange data we recorded (e.g. Hyperliquid
                   ``fundingHistory`` for ``xyz:CL``, market precision from
                   ``metaAndAssetCtxs``).
* ``ASSUMPTION`` - a modelling choice we could not verify. Results depend on it,
                   so every report shows it and the sensitivity runs vary it.

Defaults are the blueprint's conservative baseline (ADR-005): 0.10% taker fee
per fill and 0.05% adverse slippage per side, plus an explicit half-spread.
The half-spread default (1 bp) is informed by live ``impactPxs``/``l2Book``
observations of roughly 0.5-1 bp full spread during the reference session; it
is NOT a historical spread series (Hyperliquid does not provide one), so it is
an assumption. Outside the reference session spread and slippage are
multiplied by ``offhours_multiplier`` (assumption).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

from autotrader.market.models import MAX_SIGNIFICANT_FIGURES, PERP_MAX_DECIMALS


class Provenance(StrEnum):
    VERIFIED = "VERIFIED"
    ASSUMPTION = "ASSUMPTION"
    UNVERIFIED = "UNVERIFIED"


class FundingMode(StrEnum):
    #: recorded hourly rates; hours without a record are charged ``missing_funding_rate``
    RECORDED = "RECORDED"
    #: no funding at all (sensitivity only - not realistic)
    ZERO = "ZERO"
    #: every hour charged |recorded rate| against the position (stress)
    ADVERSE = "ADVERSE"


@dataclass(frozen=True)
class CostModel:
    taker_fee_rate: Decimal = Decimal("0.001")
    half_spread_rate: Decimal = Decimal("0.0001")
    slippage_rate: Decimal = Decimal("0.0005")
    offhours_multiplier: Decimal = Decimal(2)
    funding_mode: FundingMode = FundingMode.RECORDED
    funding_multiplier: Decimal = Decimal(1)
    #: charged against the position for every hour with no recorded funding
    missing_funding_rate: Decimal = Decimal("0.0000125")

    def validate(self) -> None:
        for name in ("taker_fee_rate", "half_spread_rate", "slippage_rate"):
            v = getattr(self, name)
            if not Decimal(0) <= v < Decimal("0.05"):
                raise ValueError(f"{name} must be in [0, 0.05)")
        if self.offhours_multiplier < 1:
            raise ValueError("offhours_multiplier must be >= 1")
        if self.funding_multiplier < 0 or self.missing_funding_rate < 0:
            raise ValueError("funding inputs must be non-negative")

    def scaled(
        self,
        *,
        fee: Decimal | int = 1,
        spread: Decimal | int = 1,
        slippage: Decimal | int = 1,
    ) -> CostModel:
        return replace(
            self,
            taker_fee_rate=self.taker_fee_rate * Decimal(fee),
            half_spread_rate=self.half_spread_rate * Decimal(spread),
            slippage_rate=self.slippage_rate * Decimal(slippage),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "taker_fee_rate": str(self.taker_fee_rate),
            "half_spread_rate": str(self.half_spread_rate),
            "slippage_rate": str(self.slippage_rate),
            "offhours_multiplier": str(self.offhours_multiplier),
            "funding_mode": self.funding_mode.value,
            "funding_multiplier": str(self.funding_multiplier),
            "missing_funding_rate": str(self.missing_funding_rate),
        }

    def assumptions(self) -> list[dict[str, str]]:
        """Human-readable provenance table shown in reports and the UI."""

        def pct(d: Decimal) -> str:
            return f"{d * 100:.4f}%"

        return [
            {
                "item": "taker_fee_rate",
                "value": pct(self.taker_fee_rate),
                "provenance": Provenance.ASSUMPTION,
                "note": "Blueprint baseline, applied to every fill (entries, stops and targets). "
                "Actual HIP-3 fee for xyz:CL (deployerFeeScale/growthMode) not verified.",
            },
            {
                "item": "half_spread_rate",
                "value": pct(self.half_spread_rate),
                "provenance": Provenance.ASSUMPTION,
                "note": "No historical bid/ask exists in candle data; informed by live book "
                "snapshots only.",
            },
            {
                "item": "slippage_rate",
                "value": pct(self.slippage_rate),
                "provenance": Provenance.ASSUMPTION,
                "note": "Adverse, per side, on market fills (entries, stops, time exits).",
            },
            {
                "item": "offhours_multiplier",
                "value": f"{self.offhours_multiplier}x",
                "provenance": Provenance.ASSUMPTION,
                "note": "Spread+slippage multiplier for fills while the WTI reference is closed.",
            },
            {
                "item": "funding",
                "value": f"{self.funding_mode.value} x{self.funding_multiplier}",
                "provenance": Provenance.VERIFIED
                if self.funding_mode is FundingMode.RECORDED
                else Provenance.ASSUMPTION,
                "note": "Hourly rates recorded from Hyperliquid fundingHistory for xyz:CL. "
                "Notional uses the 30m candle close at the funding hour (oracle price history "
                "is not available) - an approximation.",
            },
            {
                "item": "missing_funding_rate",
                "value": pct(self.missing_funding_rate) + "/h",
                "provenance": Provenance.ASSUMPTION,
                "note": "Charged against the position for any hour without a recorded rate.",
            },
            {
                "item": "take_profit_fill",
                "value": "limit at target, requires trade-through",
                "provenance": Provenance.ASSUMPTION,
                "note": "Target counts only if the bar trades beyond it (touch is not enough); "
                "filled at the target price even if the bar opened beyond it; taker fee charged.",
            },
        ]


# --------------------------------------------------------------------------- precision


def max_price_decimals(sz_decimals: int) -> int:
    return PERP_MAX_DECIMALS - sz_decimals


def round_price(px: Decimal, sz_decimals: int, mode: str = "nearest") -> Decimal:
    """Round to a valid Hyperliquid perp price: <= 5 significant figures and
    <= ``6 - szDecimals`` decimals (integers are always valid).

    ``mode``: "up", "down" or "nearest". Use "up" for buy fills and "down" for
    sell fills so rounding never improves a simulated fill.
    """
    if px <= 0:
        raise ValueError("price must be positive")
    rounding = {"up": ROUND_CEILING, "down": ROUND_FLOOR, "nearest": ROUND_HALF_EVEN}[mode]
    int_digits = len(str(int(px))) if px >= 1 else 0
    sig_decimals = max(MAX_SIGNIFICANT_FIGURES - int_digits, 0)
    decimals = min(max_price_decimals(sz_decimals), sig_decimals)
    out = px.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)
    if out <= 0:
        out = Decimal(1).scaleb(-decimals)
    return out


def floor_size(size: Decimal, sz_decimals: int) -> Decimal:
    if size <= 0:
        return Decimal(0)
    return size.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_FLOOR)
