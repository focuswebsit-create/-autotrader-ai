"""Persistence helpers for Phase 0 entities."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from autotrader.db.models import MarketMetadata, SystemEvent
from autotrader.market.models import MarketDiscovery


def save_discovery(session: Session, d: MarketDiscovery) -> MarketMetadata:
    row = MarketMetadata(
        dex=d.dex,
        coin=d.coin,
        dex_index=d.dex_index,
        asset_index_in_dex=d.asset_index_in_dex,
        sz_decimals=d.sz_decimals,
        max_price_decimals=d.max_price_decimals,
        max_leverage=d.max_leverage,
        only_isolated=d.only_isolated,
        margin_mode=d.margin_mode,
        mark_px=d.mark_px,
        oracle_px=d.oracle_px,
        mid_px=d.mid_px,
        funding_rate=d.funding_rate,
        open_interest=d.open_interest,
        oracle_mark_divergence_bps=d.oracle_mark_divergence_bps,
        status=d.status,
        warnings=list(d.warnings),
        payload=d.model_dump(mode="json"),
        source=d.source,
        raw_sha256=d.raw_sha256,
        discovered_at=d.discovered_at,
    )
    session.add(row)
    return row


def latest_discovery(session: Session, coin: str) -> MarketMetadata | None:
    stmt = (
        select(MarketMetadata)
        .where(MarketMetadata.coin == coin)
        .order_by(MarketMetadata.discovered_at.desc(), MarketMetadata.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def record_event(
    session: Session,
    event_type: str,
    severity: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    subject: str | None = None,
) -> SystemEvent:
    event = SystemEvent(
        event_type=event_type,
        severity=severity,
        message=message,
        details=details or {},
        subject=subject,
    )
    session.add(event)
    return event


def latest_event(
    session: Session, event_type: str, subject: str | None = None
) -> SystemEvent | None:
    """Most recent event of a type (optionally for one subject, e.g. a coin).

    Ordered by the monotonically increasing primary key, i.e. insertion order, so
    the result never depends on clock precision or worker/DB clock skew.
    """
    stmt = select(SystemEvent).where(SystemEvent.event_type == event_type)
    if subject is not None:
        stmt = stmt.where(SystemEvent.subject == subject)
    stmt = stmt.order_by(SystemEvent.id.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def db_ping(session: Session) -> bool:
    session.execute(text("SELECT 1"))
    return True
