"""Startup job: safety checks + read-only market discovery (+ optional persistence)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.common.safety import assert_paper_mode, check_environment
from autotrader.db.repository import record_event, save_discovery
from autotrader.market.discovery import DiscoveryError, InfoSource, discover_market
from autotrader.market.models import MarketDiscovery

log = logging.getLogger("autotrader.worker")


@dataclass(frozen=True)
class DiscoveryOutcome:
    ok: bool
    discovery: MarketDiscovery | None
    error_code: str | None
    error_message: str | None
    #: False when a database was configured but the result could not be stored.
    persisted: bool = True


def _persist(sessions: sessionmaker[Session], write: Callable[[Session], object]) -> bool:
    """Run ``write`` in a transaction. A DB outage is logged, never raised."""
    try:
        with sessions.begin() as s:
            write(s)
    except SQLAlchemyError as exc:
        log.error(
            "database unavailable - discovery result not persisted",
            extra={"error_type": type(exc).__name__},
        )
        return False
    return True


def run_discovery_once(
    settings: Settings,
    source: InfoSource,
    sessions: sessionmaker[Session] | None = None,
) -> DiscoveryOutcome:
    check_environment()
    mode = assert_paper_mode()
    log.info("startup safety checks passed", extra={"trading_mode": mode})

    try:
        result = discover_market(
            source,
            dex=settings.market_dex,
            coin=settings.market_coin,
            max_divergence_bps=settings.max_oracle_mark_divergence_bps,
        )
    except DiscoveryError as exc:
        code, message = str(exc.code), exc.message
        log.error(
            "market discovery failed - entries blocked (fail-closed)",
            extra={"code": code, "detail": message},
        )
        persisted = True
        if sessions is not None:
            details = {"code": code, "coin": settings.market_coin}

            def write_failure(s: Session) -> None:
                record_event(s, "MARKET_DISCOVERY_FAILED", "ERROR", message, details)

            persisted = _persist(sessions, write_failure)
        return DiscoveryOutcome(False, None, code, message, persisted)

    log.info(
        "market discovered",
        extra={
            "coin": result.coin,
            "dex_index": result.dex_index,
            "asset_index_in_dex": result.asset_index_in_dex,
            "status": result.status,
            "mark_px": str(result.mark_px),
            "oracle_px": str(result.oracle_px),
            "divergence_bps": str(result.oracle_mark_divergence_bps),
        },
    )
    persisted = True
    if sessions is not None:

        def write(s: Session) -> None:
            save_discovery(s, result)
            record_event(
                s,
                "MARKET_DISCOVERY_OK",
                "WARNING" if result.warnings else "INFO",
                f"{result.coin} discovered with status {result.status}",
                {"warnings": result.warnings, "raw_sha256": result.raw_sha256},
            )

        persisted = _persist(sessions, write)
    return DiscoveryOutcome(True, result, None, None, persisted)
