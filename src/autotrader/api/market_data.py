"""Read-only market-data API (Phase 1). GET only; every value comes from the DB.

Alert and health states are *derived* here from stored facts (heartbeat age,
last closed bar, open issues) so the UI never invents a status.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings
from autotrader.db.market_data import (
    closed_candles,
    coverage,
    forming_candle,
    get_heartbeat,
    list_issues,
)
from autotrader.db.models import CandleRow, DataQualityIssue
from autotrader.ingest.live import SERVICE_NAME
from autotrader.market.candles import CLOSE_GRACE, Interval
from autotrader.reporting.data_report import build_data_report

#: Heartbeat older than this -> collector considered DOWN.
HEARTBEAT_DOWN_AFTER = timedelta(seconds=60)
#: WS frame older than this -> live feed considered stale.
WS_STALE_AFTER = timedelta(seconds=90)
#: Allowed delay before a closed bar must be in the DB (finalize runs every 30 s).
CLOSED_BAR_LAG_TOLERANCE = timedelta(minutes=3)
MAX_CANDLES = 5_000


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    v = _utc(value)
    return v.isoformat() if v else None


def num(value: Decimal) -> str:
    """Exact decimal string without NUMERIC(38,18) padding: 94.595000... -> "94.595"."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def candle_json(row: CandleRow) -> dict[str, Any]:
    open_time = _utc(row.open_time)
    assert open_time is not None
    return {
        "t": int(open_time.timestamp()),
        "open_time": open_time.isoformat(),
        "close_time": _iso(row.close_time),
        "o": num(row.open),
        "h": num(row.high),
        "l": num(row.low),
        "c": num(row.close),
        "v": num(row.volume),
        "n": row.trades,
        "closed": row.is_closed,
        "source": row.source,
    }


def issue_json(i: DataQualityIssue) -> dict[str, Any]:
    return {
        "id": i.id,
        "interval": i.interval,
        "type": i.issue_type,
        "severity": i.severity,
        "open_time": _iso(i.open_time),
        "range_end": _iso(i.range_end),
        "missing_bars": i.missing_bars,
        "detail": i.detail,
        "occurrences": i.occurrences,
        "first_detected_at": _iso(i.first_detected_at),
        "last_detected_at": _iso(i.last_detected_at),
        "resolved_at": _iso(i.resolved_at),
    }


def expected_last_closed_open(interval: Interval, now: datetime) -> datetime:
    """Open time of the most recent bar that must be closed by ``now``."""
    ms = interval.ms
    now_ms = int((now - CLOSE_GRACE).timestamp() * 1000)
    current_open_ms = now_ms - now_ms % ms
    return datetime.fromtimestamp((current_open_ms - ms) / 1000, tz=UTC)


def collector_status(session: Session, coin: str, now: datetime) -> dict[str, Any]:
    hb = get_heartbeat(session, SERVICE_NAME)
    alerts: list[dict[str, Any]] = []

    if hb is None:
        service_state = "NOT_STARTED"
        alerts.append({"code": "COLLECTOR_NOT_STARTED", "severity": "ERROR"})
        hb_json: dict[str, Any] | None = None
    else:
        hb_age = now - (_utc(hb.last_heartbeat_at) or now)
        service_state = hb.state
        if hb.state == "STOPPED":
            alerts.append({"code": "COLLECTOR_STOPPED", "severity": "ERROR"})
        elif hb_age > HEARTBEAT_DOWN_AFTER:
            service_state = "DOWN"
            alerts.append(
                {
                    "code": "COLLECTOR_DOWN",
                    "severity": "ERROR",
                    "age_s": int(hb_age.total_seconds()),
                }
            )
        last_ws = _utc(hb.last_ws_message_at)
        ws_age = now - last_ws if last_ws is not None else None
        if service_state in ("RUNNING", "DEGRADED"):
            if hb.ws_state != "CONNECTED":
                alerts.append({"code": "WS_DISCONNECTED", "severity": "WARNING"})
            elif ws_age is not None and ws_age > WS_STALE_AFTER:
                alerts.append(
                    {
                        "code": "WS_STALE",
                        "severity": "WARNING",
                        "age_s": int(ws_age.total_seconds()),
                    }
                )
        details = hb.details or {}
        if details.get("last_sync_ok") is False:
            alerts.append(
                {
                    "code": "REST_SYNC_FAILED",
                    "severity": "WARNING",
                    "error": details.get("last_error"),
                }
            )
        hb_json = {
            "state": hb.state,
            "ws_state": hb.ws_state,
            "started_at": _iso(hb.started_at),
            "last_heartbeat_at": _iso(hb.last_heartbeat_at),
            "heartbeat_age_s": round(hb_age.total_seconds(), 1),
            "last_ws_message_at": _iso(hb.last_ws_message_at),
            "ws_message_age_s": round(ws_age.total_seconds(), 1) if ws_age is not None else None,
            "last_candle_update_at": _iso(hb.last_candle_update_at),
            "reconnects": hb.reconnects,
            "messages": details.get("messages"),
            "ignored_out_of_order": details.get("ignored_out_of_order"),
            "rejected": details.get("rejected"),
            "last_sync_at": details.get("last_sync_at"),
            "last_sync_ok": details.get("last_sync_ok"),
            "last_error": details.get("last_error"),
            "market_ctx": details.get("market_ctx") or {},
        }

    intervals = []
    for iv in Interval:
        cov = coverage(session, coin, iv)
        expected = expected_last_closed_open(iv, now)
        lag = (expected - cov.last_open_time) if cov.last_open_time else None
        fresh = lag is not None and lag <= CLOSED_BAR_LAG_TOLERANCE
        if cov.closed_count == 0:
            alerts.append({"code": "NO_DATA", "severity": "ERROR", "interval": iv.value})
        elif not fresh:
            alerts.append(
                {
                    "code": "DATA_STALE",
                    "severity": "WARNING",
                    "interval": iv.value,
                    "last_closed_open_time": _iso(cov.last_open_time),
                }
            )
        if cov.open_gaps:
            alerts.append(
                {
                    "code": "GAPS",
                    "severity": "ERROR",
                    "interval": iv.value,
                    "count": cov.open_gaps,
                    "missing_bars": cov.missing_bars,
                }
            )
        rejected = cov.open_issues.get("REJECTED_CANDLE", 0)
        if rejected:
            alerts.append(
                {
                    "code": "REJECTED_CANDLES",
                    "severity": "ERROR",
                    "interval": iv.value,
                    "count": rejected,
                }
            )
        intervals.append(
            {
                "interval": iv.value,
                "closed_candles": cov.closed_count,
                "first_open_time": _iso(cov.first_open_time),
                "last_closed_open_time": _iso(cov.last_open_time),
                "expected_last_closed_open_time": expected.isoformat(),
                "fresh": fresh,
                "missing_bars": cov.missing_bars,
                "open_gaps": cov.open_gaps,
                "open_issues": cov.open_issues,
                "forming_candle_open_time": _iso(cov.forming_open_time),
            }
        )

    overall = "OK"
    if any(a["severity"] == "ERROR" for a in alerts):
        overall = "ERROR"
    elif alerts:
        overall = "WARNING"
    return {
        "service": SERVICE_NAME,
        "service_state": service_state,
        "overall": overall,
        "heartbeat": hb_json,
        "intervals": intervals,
        "alerts": alerts,
        "checked_at": now.isoformat(),
    }


def build_router(
    settings: Settings,
    sessions_provider: Callable[[], sessionmaker[Session] | None],
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api")

    def _sessions() -> sessionmaker[Session]:
        sessions = sessions_provider()
        if sessions is None:
            raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
        return sessions

    @router.get("/candles")
    def get_candles(
        interval: Interval,
        start: Annotated[datetime | None, Query()] = None,
        end: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_CANDLES)] = 1000,
        include_forming: bool = True,
    ) -> dict[str, Any]:
        start_u, end_u = _utc(start), _utc(end)
        if start_u and end_u and start_u >= end_u:
            raise HTTPException(status_code=422, detail="START_AFTER_END")
        now = clock()
        try:
            with _sessions()() as s:
                rows = closed_candles(
                    s,
                    settings.market_coin,
                    interval,
                    as_of=now,
                    start=start_u,
                    end=end_u,
                    limit=limit,
                )
                forming = (
                    forming_candle(s, settings.market_coin, interval) if include_forming else None
                )
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc
        forming_open = _utc(forming.open_time) if forming is not None else None
        if forming_open is not None and end_u is not None and forming_open >= end_u:
            forming = None
        return {
            "coin": settings.market_coin,
            "interval": interval.value,
            "as_of": now.isoformat(),
            "count": len(rows),
            "truncated": len(rows) == limit,
            "candles": [candle_json(r) for r in rows],
            # The forming bar is for display only and is clearly separated.
            "forming": candle_json(forming) if forming is not None else None,
        }

    @router.get("/data/report")
    def get_report() -> dict[str, Any]:
        try:
            with _sessions()() as s:
                return build_data_report(s, settings.market_coin)
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/data/issues")
    def get_issues(
        interval: Interval | None = None,
        include_resolved: bool = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        try:
            with _sessions()() as s:
                rows = list_issues(
                    s,
                    settings.market_coin,
                    interval,
                    include_resolved=include_resolved,
                    limit=limit,
                )
                return {"issues": [issue_json(i) for i in rows]}
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    @router.get("/collector/status")
    def get_collector_status() -> dict[str, Any]:
        try:
            with _sessions()() as s:
                return collector_status(s, settings.market_coin, clock())
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc

    return router
