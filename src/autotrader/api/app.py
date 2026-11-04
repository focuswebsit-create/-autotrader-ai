"""FastAPI backend (read-only in Phase 0).

Every route here is a GET. There is no order / trade / exchange route and
``tests/unit/test_api.py`` asserts that.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader import __version__
from autotrader.api.backtest import build_backtest_router
from autotrader.api.forward import build_forward_router
from autotrader.api.market_data import build_router
from autotrader.api.paper import build_paper_router
from autotrader.common.config import Settings, get_settings
from autotrader.common.safety import (
    ALLOWED_INFO_TYPES,
    INFO_URL,
    assert_paper_mode,
    check_environment,
)
from autotrader.db.repository import db_ping, latest_discovery, latest_event
from autotrader.db.session import session_factory

#: Discovery older than this is reported as stale.
DISCOVERY_STALE_AFTER_S = 15 * 60


def create_app(
    settings: Settings | None = None,
    sessions: sessionmaker[Session] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> FastAPI:
    check_environment()
    mode = assert_paper_mode()
    settings = settings or get_settings()
    if sessions is None and settings.database_url:
        sessions = session_factory(settings.database_url)

    app = FastAPI(
        title="AutoTrader AI (PAPER ONLY)",
        version=__version__,
        description="Read-only research / paper-trading backend. No real orders exist here.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.include_router(build_router(settings, lambda: sessions, clock))
    app.include_router(build_backtest_router(settings, lambda: sessions))
    app.include_router(build_forward_router(settings, lambda: sessions, clock))
    app.include_router(build_paper_router(settings, lambda: sessions, clock))

    @app.get("/health")
    def health(response: Response) -> dict[str, Any]:
        db_status = "not_configured"
        if sessions is not None:
            try:
                with sessions() as s:
                    db_ping(s)
                db_status = "ok"
            except SQLAlchemyError:
                db_status = "unavailable"
        degraded = db_status == "unavailable"
        if degraded:
            # Non-2xx so container/uptime health checks notice the outage.
            response.status_code = 503
        return {
            "status": "degraded" if degraded else "ok",
            "trading_mode": mode,
            "paper_only": True,
            "database": db_status,
            "version": __version__,
            "time": datetime.now(tz=UTC).isoformat(),
        }

    @app.get("/api/system/mode")
    def system_mode() -> dict[str, Any]:
        return {
            "trading_mode": mode,
            "paper_only": True,
            "live_trading_available": False,
            "allowed_endpoint": f"POST {INFO_URL}",
            "allowed_info_types": sorted(ALLOWED_INFO_TYPES),
            "initial_equity_usdc": str(settings.initial_equity_usdc),
            "market": {"dex": settings.market_dex, "coin": settings.market_coin},
        }

    @app.get("/api/market/discovery")
    def market_discovery() -> dict[str, Any]:
        if sessions is None:
            raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
        coin = settings.market_coin
        try:
            with sessions() as s:
                row = latest_discovery(s, coin)
                ok_event = latest_event(s, "MARKET_DISCOVERY_OK", coin)
                failure = latest_event(s, "MARKET_DISCOVERY_FAILED", coin)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc
        if row is None:
            if failure is not None:
                # Never succeeded: surface the real reason instead of "not yet".
                raise HTTPException(status_code=503, detail=failure.details.get("code"))
            raise HTTPException(status_code=404, detail="NO_DISCOVERY_YET")
        now = datetime.now(tz=UTC)
        discovered_at = row.discovered_at
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)
        age_s = max((now - discovered_at).total_seconds(), 0.0)
        # Ordering by insertion id (same table, same sequence) rather than by
        # timestamps: immune to clock precision and worker/DB clock skew.
        last_failure = None
        if failure is not None and (ok_event is None or failure.id > ok_event.id):
            occurred = failure.occurred_at
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=UTC)
            last_failure = {
                "occurred_at": occurred.isoformat(),
                "code": failure.details.get("code"),
                "message": failure.message,
            }
        stale = age_s > DISCOVERY_STALE_AFTER_S
        if last_failure is not None:
            state = "FAILED"
        elif stale:
            state = "STALE"
        else:
            state = "CURRENT"
        return {
            "discovery": row.payload,
            # CURRENT: latest cycle succeeded and is fresh. STALE/FAILED: the values
            # below are the last known ones and must not be presented as live.
            "state": state,
            "age_seconds": round(age_s, 1),
            "stale": stale,
            "last_failure_after_discovery": last_failure,
            "entries_allowed": False,  # Phase 0: no strategy/paper engine yet
            "entries_blocked_reason": "PHASE_0_NO_STRATEGY",
        }

    return app
