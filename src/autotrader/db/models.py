"""SQLAlchemy 2 ORM models (Phase 0 + Phase 1 subset of the blueprint schema).

All timestamps are timezone-aware UTC. Monetary/price values use NUMERIC.
Further tables (signals, paper_* ...) arrive in later phases via new Alembic
revisions. Every table must be granted to the DB roles (see migration 0003).
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

JsonType = JSON().with_variant(JSONB(), "postgresql")


class _ExactDecimalText(TypeDecorator[Decimal]):
    """SQLite-only stand-in for NUMERIC: SQLite stores NUMERIC as binary float,
    which silently changes prices (96.406 -> 96.406000000000005912). Storing the
    exact decimal text keeps unit tests faithful to PostgreSQL semantics."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else format(Decimal(value), "f")

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


Price = Numeric(38, 18).with_variant(_ExactDecimalText(), "sqlite")


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
        CheckConstraint("mark_px > 0 AND oracle_px > 0", name="ck_market_metadata_prices").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_market_metadata_coin_discovered", "coin", "discovered_at"),
    )


class SystemEvent(Base):
    """Operational events (startup, discovery success/failure, halts...)."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: What the event is about (e.g. the market "xyz:CL"); NULL for system-wide events.
    subject: Mapped[str | None] = mapped_column(String(64))
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
        Index("ix_system_events_type_subject_id", "event_type", "subject", "id"),
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


# --------------------------------------------------------------------------- Phase 1
PK = BigInteger().with_variant(Integer, "sqlite")


class CandleRow(Base):
    """OHLCV bars. One row per (coin, interval, open_time): writes are idempotent upserts.

    ``is_closed`` is false while the bar is still forming. Strategy/backtest code
    must only read closed bars via ``closed_candles(..., as_of=...)``.
    """

    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Price, nullable=False)
    high: Mapped[Decimal] = mapped_column(Price, nullable=False)
    low: Mapped[Decimal] = mapped_column(Price, nullable=False)
    close: Mapped[Decimal] = mapped_column(Price, nullable=False)
    volume: Mapped[Decimal] = mapped_column(Price, nullable=False)
    trades: Mapped[int | None] = mapped_column(Integer)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: "rest" (candleSnapshot) or "ws" (live stream). Closed bars are always
    #: re-confirmed from REST, which is treated as the source of truth.
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingestion_run_id: Mapped[int | None] = mapped_column(PK)

    __table_args__ = (
        UniqueConstraint("coin", "interval", "open_time", name="uq_candles_bar"),
        CheckConstraint("interval IN ('30m','1h')", name="ck_candles_interval"),
        CheckConstraint("source IN ('rest','ws')", name="ck_candles_source"),
        # Numeric checks only make sense where prices are NUMERIC (PostgreSQL).
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_candles_positive"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "high >= open AND high >= close AND low <= open AND low <= close AND high >= low",
            name="ck_candles_ohlc",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("volume >= 0", name="ck_candles_volume").ddl_if(dialect="postgresql"),
        CheckConstraint("close_time > open_time", name="ck_candles_times"),
        Index("ix_candles_lookup", "coin", "interval", "is_closed", "open_time"),
    )


class DataQualityIssue(Base):
    """Every data problem we detect is recorded here - never silently fixed."""

    __tablename__ = "data_quality_issues"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Bar the issue refers to (for a GAP: first missing bar).
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: For a GAP: open time of the first bar after the gap.
    range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_bars: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[Any | None] = mapped_column(JsonType)
    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Set when a later check shows the problem is gone (e.g. a gap later filled by
    #: the exchange's own data). The row is kept for the audit trail.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "coin", "interval", "issue_type", "open_time", name="uq_data_quality_issue"
        ),
        CheckConstraint("severity IN ('INFO','WARNING','ERROR')", name="ck_dqi_severity"),
        Index("ix_dqi_open", "coin", "interval", "resolved_at"),
    )


class IngestionRun(Base):
    """Provenance of every REST fetch: what was asked, what came back, what changed."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('backfill','gap_fill','finalize')", name="ck_ingest_kind"),
        CheckConstraint("status IN ('RUNNING','OK','FAILED')", name="ck_ingest_status"),
        Index("ix_ingestion_runs_recent", "coin", "interval", "started_at"),
    )


class ServiceHeartbeat(Base):
    """Latest self-reported state of each long-running service (one row per service)."""

    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    ws_state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ws_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_candle_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "state IN ('STARTING','RUNNING','DEGRADED','STOPPED')", name="ck_heartbeat_state"
        ),
        CheckConstraint(
            "ws_state IN ('DISCONNECTED','CONNECTING','CONNECTED','STALE')",
            name="ck_heartbeat_ws_state",
        ),
    )
