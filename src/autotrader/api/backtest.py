"""Read-only backtest API (Phase 2). GET only: runs are produced by the CLI
(``autotrader backtest``) and stored immutably; the API never starts a run and
never touches any trading endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.backtest.store import get_run, list_runs, run_header
from autotrader.common.config import Settings
from autotrader.strategy.strategies import STRATEGIES


def build_backtest_router(
    settings: Settings, sessions_provider: Callable[[], sessionmaker[Session] | None]
) -> APIRouter:
    router = APIRouter(prefix="/api/backtest")

    def _sessions() -> sessionmaker[Session]:
        sessions = sessions_provider()
        if sessions is None:
            raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
        return sessions

    @router.get("/strategies")
    def strategies() -> dict[str, Any]:
        return {
            "strategies": [
                {
                    "id": sid,
                    "version": cls.version,
                    "title_he": cls.title_he,
                    "description_he": cls.description_he,
                    "default_params": pcls().as_dict(),
                }
                for sid, (cls, pcls) in STRATEGIES.items()
            ]
        }

    @router.get("/runs")
    def runs(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        try:
            with _sessions()() as s:
                rows = list_runs(s, settings.market_coin, limit)
                return {"runs": [run_header(r) for r in rows]}
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/runs/{run_id}")
    def run(run_id: Annotated[int, Path(ge=1, le=2**62)]) -> dict[str, Any]:
        try:
            with _sessions()() as s:
                r = get_run(s, run_id)
                if r is None or r.coin != settings.market_coin:
                    raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
                return {"header": run_header(r), "report": r.report}
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    return router
