"""Startup job: safety checks + read-only market discovery (+ optional persistence).

Every cycle ends in exactly one of: a stored success (``MARKET_DISCOVERY_OK``), a
stored failure (``MARKET_DISCOVERY_FAILED``), or, if the database itself is down,
nothing stored and ``persisted=False``. Unexpected errors are classified as
failures, so the API never keeps presenting an older success as current.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.common.safety import SafetyViolationError, assert_paper_mode, check_environment
from autotrader.db.repository import record_event, save_discovery
from autotrader.market.discovery import DiscoveryError, InfoSource, discover_market
from autotrader.market.models import MarketDiscovery

log = logging.getLogger("autotrader.worker")

EVENT_OK = "MARKET_DISCOVERY_OK"
EVENT_FAILED = "MARKET_DISCOVERY_FAILED"
#: Code used when the discovery result was valid but the DB refused to store it.
CODE_DB_REJECTED = "DB_REJECTED"
#: Code used for unexpected (non-classified) errors during discovery.
CODE_UNEXPECTED = "UNEXPECTED_ERROR"

PersistResult = Literal["ok", "db_unavailable", "db_rejected"]


@dataclass(frozen=True)
class DiscoveryOutcome:
    ok: bool
    discovery: MarketDiscovery | None
    error_code: str | None
    error_message: str | None
    #: False when a database was configured but the outcome could not be stored.
    persisted: bool = True


def _is_outage(exc: SQLAlchemyError) -> bool:
    """Connection-level failure (retry later) vs. the DB rejecting our data."""
    if isinstance(exc, OperationalError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def _persist(sessions: sessionmaker[Session], write: Callable[[Session], object]) -> PersistResult:
    """Run ``write`` in a transaction. Database problems are logged, never raised."""
    try:
        with sessions.begin() as s:
            write(s)
    except SQLAlchemyError as exc:
        if _is_outage(exc):
            log.error("database unavailable - not persisted", extra={"error": type(exc).__name__})
            return "db_unavailable"
        log.error("database rejected the data - not persisted", extra={"error": type(exc).__name__})
        return "db_rejected"
    return "ok"


def _record_failure(
    settings: Settings,
    sessions: sessionmaker[Session] | None,
    code: str,
    message: str,
) -> DiscoveryOutcome:
    log.error(
        "market discovery failed - entries blocked (fail-closed)",
        extra={"code": code, "detail": message},
    )
    persisted = True
    if sessions is not None:
        coin = settings.market_coin
        details = {"code": code, "coin": coin}

        def write_failure(s: Session) -> None:
            record_event(s, EVENT_FAILED, "ERROR", message, details, subject=coin)

        persisted = _persist(sessions, write_failure) == "ok"
    return DiscoveryOutcome(False, None, code, message, persisted)


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
        return _record_failure(settings, sessions, str(exc.code), exc.message)
    except SafetyViolationError:
        raise
    except Exception as exc:
        # Unknown failure mode: still a failure (fail closed), and it must be visible.
        log.exception("unexpected error during discovery")
        return _record_failure(settings, sessions, CODE_UNEXPECTED, f"{type(exc).__name__}")

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
    if sessions is None:
        return DiscoveryOutcome(True, result, None, None, True)

    def write(s: Session) -> None:
        save_discovery(s, result)
        record_event(
            s,
            EVENT_OK,
            "WARNING" if result.warnings else "INFO",
            f"{result.coin} discovered with status {result.status}",
            {"warnings": result.warnings, "raw_sha256": result.raw_sha256},
            subject=result.coin,
        )

    persist = _persist(sessions, write)
    if persist == "db_rejected":
        # The data was valid for discovery but not storable: never leave the
        # previous success looking current.
        return _record_failure(
            settings, sessions, CODE_DB_REJECTED, "discovery result rejected by the database"
        )
    return DiscoveryOutcome(True, result, None, None, persist == "ok")
