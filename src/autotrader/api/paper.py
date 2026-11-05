"""Read-only PAPER trading endpoints for the terminal (GET only, DB-backed).

Starting / stopping the bot is NOT here: that is the authenticated Control API
(a separate service). These routes only read the ledger the paper worker wrote.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.paper import engine, report

AccountName = Annotated[str, Query(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def build_paper_router(
    settings: Settings,
    sessions_provider: Callable[[], sessionmaker[Session] | None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/paper")

    def run(account: str, fn: Callable[[Session, Any], Any]) -> Any:
        sessions = sessions_provider()
        if sessions is None:
            raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
        try:
            with sessions() as s:
                acc = report.get_account(s, settings.market_coin, account)
                if acc is None:
                    raise HTTPException(status_code=404, detail="PAPER_ACCOUNT_NOT_FOUND")
                return fn(s, acc)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/health")
    def health(response: Response) -> dict[str, Any]:
        """Uptime monitoring of the PAPER trader (used by infra/deploy/monitor.sh).

        503 when the paper worker is down while an account exists, or a bot is HALTED
        (a risk limit or the kill switch fired: the owner must review). 200 otherwise.
        """
        sessions = sessions_provider()
        now = clock()
        if sessions is None:
            response.status_code = 503
            return {"status": "DOWN", "alerts": [{"code": "DATABASE_NOT_CONFIGURED"}]}
        alerts: list[dict[str, str]] = []
        try:
            with sessions() as s:
                accs = list(engine.accounts(s, settings.market_coin))
                quote = report.worker_quote(s, now)
                states = {a.name: engine.current_state(s, a.id) for a in accs}
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "DOWN", "alerts": [{"code": "DATABASE_UNAVAILABLE"}]}
        if not accs:
            return {"status": "NOT_CONFIGURED", "alerts": [], "accounts": {}}
        if quote["worker"] in ("NOT_RUNNING", "DOWN", "STOPPED"):
            alerts.append({"code": "PAPER_WORKER_DOWN", "severity": "ERROR"})
        for name, ev in sorted(states.items()):
            if ev.state == "HALTED":
                alerts.append({"code": f"PAPER_{name.upper()}_HALTED:{ev.reason}",
                               "severity": "ERROR"})  # fmt: skip
            elif ev.state == "DEGRADED":
                alerts.append({"code": f"PAPER_{name.upper()}_DEGRADED:{ev.reason}",
                               "severity": "WARNING"})  # fmt: skip
        errors = [a for a in alerts if a["severity"] == "ERROR"]
        if errors:
            response.status_code = 503
        status = "DOWN" if errors else ("DEGRADED" if alerts else "OK")
        return {
            "status": status,
            "alerts": alerts,
            "accounts": {n: ev.state for n, ev in states.items()},
            "worker": quote["worker"],
        }

    @router.get("/summary")
    def summary(account: AccountName = "demo") -> dict[str, Any]:
        return run(account, lambda s, a: report.summary(s, a, clock()))  # type: ignore[no-any-return]

    @router.get("/trades")
    def trades(
        account: AccountName = "demo", period: Literal["day", "week", "month", "all"] = "all"
    ) -> dict[str, Any]:
        return {"trades": run(account, lambda s, a: report.trades(s, a, clock(), period))}

    @router.get("/decisions")
    def decisions(
        account: AccountName = "demo", limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> dict[str, Any]:
        return {"decisions": run(account, lambda s, a: report.decisions(s, a, limit))}

    @router.get("/events")
    def events(
        account: AccountName = "demo", limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> dict[str, Any]:
        return {"events": run(account, lambda s, a: report.events(s, a, limit))}

    @router.get("/equity")
    def equity(account: AccountName = "demo") -> dict[str, Any]:
        return run(account, lambda s, a: report.equity_curve(s, a))  # type: ignore[no-any-return]

    return router
