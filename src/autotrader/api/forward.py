"""Read-only forward-test API (Phase 2.5). GET only: tests are frozen via the CLI,
decisions are written by the forward worker, results are recomputed per request."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.api.market_data import collector_status
from autotrader.common.config import Settings
from autotrader.db.market_data import get_heartbeat
from autotrader.db.models import ForwardDecision, ForwardTest
from autotrader.forward.evaluate import _decision_json, evaluate
from autotrader.forward.recorder import test_state
from autotrader.forward.worker import SERVICE_NAME as FORWARD_SERVICE

#: worker ticks every 30 s; older than this = down
FORWARD_HEARTBEAT_MAX_S = 180


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

    @router.get("/health")
    def health(response: Response) -> dict[str, Any]:
        """One endpoint for uptime monitoring of the whole forward pipeline.

        503 + alerts when: DB down, collector not running / WS down / data stale,
        forward worker heartbeat stale, a test halted, or recording lags the data.
        """
        now = clock()
        alerts: list[dict[str, str]] = []
        try:
            with _sessions()() as s:
                collector = collector_status(s, settings.market_coin, now)
                hb = get_heartbeat(s, FORWARD_SERVICE)
                tests = list(
                    s.scalars(select(ForwardTest).where(ForwardTest.coin == settings.market_coin))
                )
                states = {t.id: test_state(s, t.id) for t in tests}
                lags: dict[int, float] = {}
                last_closed = next(
                    (
                        i.get("last_closed_open_time")
                        for i in collector.get("intervals", [])
                        if i.get("interval") == "30m"
                    ),
                    None,
                )
                for t in tests:
                    if states[t.id] != "ACTIVE":
                        continue
                    last = s.scalar(
                        select(func.max(ForwardDecision.bar_close_time)).where(
                            ForwardDecision.test_id == t.id
                        )
                    )
                    ref = max(t.starts_at, last) if last else t.starts_at
                    lags[t.id] = (now - ref.astimezone(UTC)).total_seconds() / 60
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "DOWN", "alerts": [{"code": "DATABASE_UNAVAILABLE"}]}
        for a in collector.get("alerts", []):
            alerts.append({"code": f"COLLECTOR:{a.get('code')}", "severity": a.get("severity", "")})
        if hb is None:
            alerts.append({"code": "FORWARD_WORKER_NOT_STARTED", "severity": "ERROR"})
        else:
            age = (now - hb.last_heartbeat_at.astimezone(UTC)).total_seconds()
            if hb.state == "STOPPED" or age > FORWARD_HEARTBEAT_MAX_S:
                alerts.append({"code": "FORWARD_WORKER_DOWN", "severity": "ERROR"})
        # Alert only on the LATEST test of each strategy: a halted test that has been
        # superseded by a newer freeze stays visible in reports but must not keep the
        # pipeline "DOWN" forever (that would mask real outages).
        latest: dict[str, int] = {}
        for t in tests:
            latest[t.strategy_id] = max(latest.get(t.strategy_id, 0), t.id)
        for sid, tid in sorted(latest.items()):
            if states[tid] == "INTEGRITY_HALT":
                alerts.append({"code": f"FORWARD_TEST_{tid}_INTEGRITY_HALT", "severity": "ERROR"})
            elif states[tid] == "STOPPED":
                alerts.append({"code": f"FORWARD_{sid.upper()}_NOT_RUNNING", "severity": "WARNING"})
        for tid, minutes in lags.items():
            # a decided bar closes every 30 min: > 75 min since the last one = not recording
            if minutes > 75:
                alerts.append({"code": f"FORWARD_TEST_{tid}_NOT_RECORDING", "severity": "ERROR"})
        errors = [a for a in alerts if a["severity"] == "ERROR"]
        if errors:
            response.status_code = 503
        return {
            "status": "DOWN" if errors else ("DEGRADED" if alerts else "OK"),
            "time": now.isoformat(),
            "alerts": alerts,
            "forward_worker": None
            if hb is None
            else {
                "state": hb.state,
                "last_heartbeat_at": hb.last_heartbeat_at.astimezone(UTC).isoformat(),
                "details": hb.details,
            },
            "tests": {str(k): v for k, v in states.items()},
            "minutes_since_last_decided_bar": {str(k): round(v, 1) for k, v in lags.items()},
            "last_closed_30m_open_time": last_closed,
            "paper_only": True,
        }

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
