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
    ForeignKey,
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
            "ws_state IN ('DISCONNECTED','CONNECTING','CONNECTED','STALE','NOT_APPLICABLE')",
            name="ck_heartbeat_ws_state",
        ),
    )


# --------------------------------------------------------------------------- Phase 2


class FundingRate(Base):
    """Recorded hourly funding for a market (Hyperliquid ``fundingHistory``). Append-only."""

    __tablename__ = "funding_rates"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    #: exact exchange timestamp of the funding event
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: the funding hour it belongs to (time floored to the hour)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Price, nullable=False)
    premium: Mapped[Decimal | None] = mapped_column(Price)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("coin", "hour", name="uq_funding_rates_hour"),
        CheckConstraint("rate > -1 AND rate < 1", name="ck_funding_rate_range").ddl_if(
            dialect="postgresql"
        ),
        Index("ix_funding_rates_lookup", "coin", "hour"),
    )


class BacktestRun(Base):
    """Immutable record of one full research-protocol run (config + data + report)."""

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    data_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    data_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('PASS','FAIL','INCONCLUSIVE','INVALID')", name="ck_backtest_runs_verdict"
        ),
        Index("ix_backtest_runs_strategy", "coin", "strategy_id", "created_at"),
    )


class BacktestTrade(Base):
    """Trades of a run's PRIMARY backtest (queryable copy of the report's trade list)."""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        PK, ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False
    )
    trade_no: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    size: Mapped[Decimal] = mapped_column(Price, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Price, nullable=False)
    r_multiple: Mapped[Decimal] = mapped_column(Price, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "trade_no", name="uq_backtest_trades_no"),
        CheckConstraint("side IN ('LONG','SHORT')", name="ck_backtest_trades_side"),
        CheckConstraint("exit_time >= entry_time", name="ck_backtest_trades_times"),
        CheckConstraint("entry_time >= signal_time", name="ck_backtest_trades_no_lookahead"),
    )


# --------------------------------------------------------------------------- Phase 2.5


class ForwardTest(Base):
    """A frozen forward test: strategy, parameters, costs, risk and code are fixed at
    creation and can never change (UPDATE/DELETE rejected by trigger)."""

    __tablename__ = "forward_tests"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: sha256 of the strategy/engine source files at freeze time
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: full frozen configuration (params, costs, risk, execution, thresholds, reference)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: indicator history always starts here (makes every decision reproducible)
    history_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: first decision is for the 30m bar opening at this instant (after creation)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("starts_at >= created_at", name="ck_forward_tests_future_start"),
        CheckConstraint("history_anchor < starts_at", name="ck_forward_tests_anchor"),
        Index("ix_forward_tests_strategy", "coin", "strategy_id"),
    )


class ForwardDecision(Base):
    """One strategy decision per closed 30m bar of a forward test (append-only)."""

    __tablename__ = "forward_decisions"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    test_id: Mapped[int] = mapped_column(
        PK, ForeignKey("forward_tests.id", ondelete="RESTRICT"), nullable=False
    )
    bar_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: wall-clock time the decision was made and stored
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: evaluated well after the bar closed (catch-up after an outage)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    atr: Mapped[Decimal | None] = mapped_column(Price)
    rationale: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    market: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: sha256 of the input bars the decision used
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("test_id", "bar_open_time", name="uq_forward_decisions_bar"),
        CheckConstraint("side IS NULL OR side IN ('LONG','SHORT')", name="ck_fd_side"),
        CheckConstraint("evaluated_at >= bar_close_time", name="ck_fd_no_lookahead"),
        Index("ix_forward_decisions_signals", "test_id", "side", "bar_open_time"),
    )


class ForwardEvent(Base):
    """Lifecycle and integrity events of a forward test (append-only)."""

    __tablename__ = "forward_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    test_id: Mapped[int] = mapped_column(
        PK, ForeignKey("forward_tests.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event IN ('STARTED','STOPPED','INTEGRITY_HALT','DATA_GAP','DATA_STALE')",
            name="ck_forward_events_event",
        ),
        Index("ix_forward_events_test", "test_id", "id"),
    )


# --------------------------------------------------------------------------- Phase 3
# Autonomous PAPER trading. Every table is append-only (UPDATE/DELETE/TRUNCATE rejected
# by triggers) except auth_sessions.revoked_at. Nothing here can reach an exchange.

PAPER_BOT_STATES = ("STOPPED", "STARTING", "RUNNING", "PAUSED", "DEGRADED", "HALTED")


class PaperAccount(Base):
    """A virtual account with its frozen strategy, risk and execution configuration."""

    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    coin: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_equity: Mapped[Decimal] = mapped_column(Price, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperCommand(Base):
    """Owner commands (from the authenticated Control API) for the paper worker."""

    __tablename__ = "paper_commands"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: client-supplied key: a double click / retried request creates one command only
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "command IN ('START','STOP','PAUSE','RESUME','KILL','CLOSE_AND_STOP','RESET_HALT')",
            name="ck_paper_commands_command",
        ),
        Index("ix_paper_commands_account", "account_id", "id"),
    )


class PaperBotEvent(Base):
    """Bot state machine transitions; the current state is the latest row."""

    __tablename__ = "paper_bot_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    command_id: Mapped[int | None] = mapped_column(PK, ForeignKey("paper_commands.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state IN ('STOPPED','STARTING','RUNNING','PAUSED','DEGRADED','HALTED')",
            name="ck_paper_bot_events_state",
        ),
        Index("ix_paper_bot_events_account", "account_id", "id"),
    )


class PaperDecision(Base):
    """One strategy decision per closed 30m bar while the bot runs (append-only)."""

    __tablename__ = "paper_decisions"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    bar_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    #: what the engine did with it: ORDER / BLOCKED / NO_SIGNAL / IN_POSITION
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    block_reason: Mapped[str | None] = mapped_column(String(64))
    atr: Mapped[Decimal | None] = mapped_column(Price)
    rationale: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    market: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "bar_open_time", name="uq_paper_decisions_bar"),
        CheckConstraint("side IS NULL OR side IN ('LONG','SHORT')", name="ck_pd_side"),
        CheckConstraint("evaluated_at >= bar_close_time", name="ck_pd_no_lookahead"),
        CheckConstraint(
            "outcome IN ('ORDER','BLOCKED','NO_SIGNAL','IN_POSITION')", name="ck_pd_outcome"
        ),
    )


class PaperOrder(Base):
    """A simulated order, stored once in its final state together with its fill."""

    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    #: deterministic id (decision / position + purpose): a retry can't fill twice
    client_order_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    requested_size: Mapped[Decimal] = mapped_column(Price, nullable=False)
    filled_size: Mapped[Decimal] = mapped_column(Price, nullable=False)
    avg_price: Mapped[Decimal | None] = mapped_column(Price)
    reference_price: Mapped[Decimal | None] = mapped_column(Price)
    fee: Mapped[Decimal] = mapped_column(Price, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    book: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('ENTRY','EXIT_STOP','EXIT_TARGET','EXIT_MANUAL','EXIT_KILL')",
            name="ck_po_purpose",
        ),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_po_side"),
        CheckConstraint("status IN ('FILLED','PARTIALLY_FILLED','REJECTED')", name="ck_po_status"),
        CheckConstraint("filled_size >= 0 AND filled_size <= requested_size", name="ck_po_size"),
        Index("ix_paper_orders_account", "account_id", "id"),
    )


class PaperPosition(Base):
    """An opened virtual position (append-only; closing writes paper_position_closes)."""

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    decision_id: Mapped[int] = mapped_column(PK, ForeignKey("paper_decisions.id"), nullable=False)
    entry_order_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_orders.id"), nullable=False, unique=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size: Mapped[Decimal] = mapped_column(Price, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    stop: Mapped[Decimal] = mapped_column(Price, nullable=False)
    target: Mapped[Decimal] = mapped_column(Price, nullable=False)
    atr: Mapped[Decimal] = mapped_column(Price, nullable=False)
    risk_budget: Mapped[Decimal] = mapped_column(Price, nullable=False)
    planned_loss: Mapped[Decimal] = mapped_column(Price, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("side IN ('LONG','SHORT')", name="ck_pp_side"),
        CheckConstraint("size > 0 AND entry_price > 0", name="ck_pp_positive"),
        Index("ix_paper_positions_account", "account_id", "id"),
    )


class PaperPositionClose(Base):
    __tablename__ = "paper_position_closes"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    position_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_positions.id"), nullable=False, unique=True
    )
    exit_order_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_orders.id"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Price, nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(Price, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Price, nullable=False)
    funding: Mapped[Decimal] = mapped_column(Price, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Price, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperLedger(Base):
    """USDC movements of a virtual account. Cash = sum(amount). Append-only."""

    __tablename__ = "paper_ledger"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Price, nullable=False)
    #: what produced the entry (e.g. "order:12", "funding:7:2026-11-04T13:00") - unique
    ref: Mapped[str] = mapped_column(String(96), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "kind", "ref", name="uq_paper_ledger_ref"),
        CheckConstraint(
            "kind IN ('DEPOSIT','FEE','REALIZED_PNL','FUNDING')", name="ck_paper_ledger_kind"
        ),
        Index("ix_paper_ledger_account", "account_id", "id"),
    )


class PaperRiskEvent(Base):
    """Every blocked entry, halt and risk-limit trigger, with its reason."""

    __tablename__ = "paper_risk_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_paper_risk_events_account", "account_id", "id"),)


class PaperEquitySnapshot(Base):
    """Equity marked at the executable price, every few minutes (curve, daily P&L)."""

    __tablename__ = "paper_equity_snapshots"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        PK, ForeignKey("paper_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Price, nullable=False)
    unrealized: Mapped[Decimal] = mapped_column(Price, nullable=False)
    equity: Mapped[Decimal] = mapped_column(Price, nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "at", name="uq_paper_equity_at"),
        Index("ix_paper_equity_account", "account_id", "at"),
    )


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: scrypt$n$r$p$salt$hash (stdlib hashlib.scrypt)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("role IN ('ADMIN','VIEWER')", name="ck_auth_users_role"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    #: sha256 of the session token (the token itself is never stored)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(PK, ForeignKey("auth_users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"

    id: Mapped[int] = mapped_column(PK, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    client_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_auth_login_attempts", "username", "at"),)
