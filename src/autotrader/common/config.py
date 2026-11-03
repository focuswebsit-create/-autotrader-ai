"""Runtime settings. Trading mode is fixed to PAPER and cannot be changed by env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from autotrader.common.safety import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOTRADER_", extra="ignore", frozen=True)

    #: Literal["PAPER"]: any other value fails validation at startup.
    trading_mode: TradingMode = "PAPER"

    database_url: str | None = Field(
        default=None, description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pw@host/db"
    )
    market_dex: str = Field(default="xyz", pattern=r"^[a-z0-9]{1,16}$")
    market_coin: str = Field(default="xyz:CL", pattern=r"^[a-z0-9]{1,16}:[A-Z0-9]{1,20}$")
    initial_equity_usdc: Decimal = Field(default=Decimal("1000"), gt=0)
    max_oracle_mark_divergence_bps: Decimal = Field(default=Decimal("50"), gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://localhost:3000"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
