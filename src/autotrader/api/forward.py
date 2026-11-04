"""Read-only forward-test API (Phase 2.5). GET only: tests are frozen via the CLI,
decisions are written by the forward worker, results are recomputed per request."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.db.models import ForwardDecision, ForwardTest
from autotrader.forward.evaluate import _decision_json, evaluate
from autotrader.forward.recorder import test_state


def build_forward_router(
    settings: Settings,
    sessions_provider: Callable[[], sessionmaker[Session] | None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/forward")

    def _sessions() -> sessionmaker[Session]:
        sessions = sessions_provider()
        if sessions is None:
            raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
        return sessions

    def _get(s: Session, test_id: int) -> ForwardTest:
        t = s.get(ForwardTest, test_id)
        if t is None or t.coin != settings.market_coin:
            raise HTTPException(status_code=404, detail="FORWARD_TEST_NOT_FOUND")
        return t

    @router.get("/tests")
    def tests() -> dict[str, Any]:
        try:
            with _sessions()() as s:
                rows = s.scalars(
                    select(ForwardTest)
                    .where(ForwardTest.coin == settings.market_coin)
                    .order_by(ForwardTest.id.desc())
                )
                return {
                    "tests": [
                        {
                            "id": t.id,
                            "strategy_id": t.strategy_id,
                            "title_he": t.config["strategy"].get("title_he"),
                            "strategy_version": t.strategy_version,
                            "params_hash": t.params_hash,
                            "state": test_state(s, t.id),
                            "starts_at": t.starts_at.astimezone(UTC).isoformat(),
                            "created_at": t.created_at.astimezone(UTC).isoformat(),
                        }
                        for t in rows
                    ]
                }
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/tests/{test_id}")
    def report(test_id: Annotated[int, Path(ge=1, le=2**62)]) -> dict[str, Any]:
        try:
            with _sessions()() as s:
                return evaluate(s, _get(s, test_id), now=clock())
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/tests/{test_id}/decisions")
    def decisions(
        test_id: Annotated[int, Path(ge=1, le=2**62)],
        signals_only: bool = True,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    ) -> dict[str, Any]:
        try:
            with _sessions()() as s:
                _get(s, test_id)
                q = select(ForwardDecision).where(ForwardDecision.test_id == test_id)
                if signals_only:
                    q = q.where(ForwardDecision.side.is_not(None))
                rows = s.scalars(q.order_by(ForwardDecision.bar_open_time.desc()).limit(limit))
                return {"decisions": [_decision_json(r) for r in rows]}
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    return router
