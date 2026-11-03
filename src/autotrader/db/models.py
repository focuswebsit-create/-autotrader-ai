"""SQLAlchemy 2 ORM models (Phase 0 subset of the blueprint schema).

All timestamps are timezone-aware UTC. Monetary/price values use NUMERIC.
Further tables (candles, signals, paper_* ...) arrive in later phases via new
Alembic revisions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")
Price = Numeric(38, 18)


class Base(DeclarativeBase):
    pass


class MarketMetadata(Base):
    """Append-only snapshots of runtime market discovery (provenance for every run)."""

    __tablename__ = "market_metadata"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    dex: Mapped[str] = mapped_column(String(32), nullable=False)
    coin: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dex_index: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_index_in_dex: Mapped[int] = mapped_column(Integer, nullable=False)
    sz_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    max_price_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    max_leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    only_isolated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    margin_mode: Mapped[str | None] = mapped_column(String(32))
    mark_px: Mapped[Decimal] = mapped_column(Price, nullable=False)
    oracle_px: Mapped[Decimal] = mapped_column(Price, nullable=False)
    mid_px: Mapped[Decimal | None] = mapped_column(Price)
    funding_rate: Mapped[Decimal] = mapped_column(Price, nullable=False)
    open_interest: Mapped[Decimal] = mapped_column(Price, nullable=False)
    oracle_mark_divergence_bps: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','DEGRADED')", name="ck_market_metadata_status"),
        CheckConstraint("mark_px > 0 AND oracle_px > 0", name="ck_market_metadata_prices"),
        Index("ix_market_metadata_coin_discovered", "coin", "discovered_at"),
    )


class SystemEvent(Base):
    """Operational events (startup, discovery success/failure, halts...)."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        CheckConstraint(
            "severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')",
            name="ck_system_events_severity",
        ),
    )


class AuditLog(Base):
    """Immutable audit trail of configuration and state changes."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    after: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemConfig(Base):
    """Key/value restricted config. ``paper_only`` row is enforced to be true."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JsonType, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="system", server_default="system"
    )

    __table_args__ = (
        CheckConstraint(
            "key <> 'paper_only' OR value = 'true'::jsonb", name="ck_system_config_paper_only"
        ).ddl_if(dialect="postgresql"),
    )
