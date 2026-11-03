"""API surface: read-only, PAPER-labelled, state derived from storage (SQLite here)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from autotrader.api.app import create_app
from autotrader.common.config import Settings
from autotrader.common.safety import SafetyViolationError
from autotrader.db.models import Base
from autotrader.worker.discovery_job import run_discovery_once
from tests.conftest import FakeInfo


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_all_routes_are_read_only(sessions: sessionmaker[Session]) -> None:
    app = create_app(Settings(), sessions)
    for route in app.routes:
        if isinstance(route, APIRoute):
            assert route.methods <= {"GET", "HEAD"}, route.path
            for word in ("order", "exchange", "trade", "withdraw", "wallet", "key"):
                assert word not in route.path.lower(), route.path


def test_health_reports_paper(sessions: sessionmaker[Session]) -> None:
    client = TestClient(create_app(Settings(), sessions))
    body = client.get("/health").json()
    assert body["trading_mode"] == "PAPER"
    assert body["paper_only"] is True
    assert body["database"] == "ok"


def test_health_without_db() -> None:
    body = TestClient(create_app(Settings(database_url=None))).get("/health").json()
    assert body["database"] == "not_configured"


def test_mode_endpoint(sessions: sessionmaker[Session]) -> None:
    body = TestClient(create_app(Settings(), sessions)).get("/api/system/mode").json()
    assert body["live_trading_available"] is False
    assert body["market"]["coin"] == "xyz:CL"
    assert body["initial_equity_usdc"] == "1000"


def test_discovery_404_until_worker_runs(sessions: sessionmaker[Session]) -> None:
    client = TestClient(create_app(Settings(), sessions))
    assert client.get("/api/market/discovery").status_code == 404


def test_discovery_after_worker(sessions: sessionmaker[Session], fake_info: FakeInfo) -> None:
    outcome = run_discovery_once(Settings(), fake_info, sessions)
    assert outcome.ok
    client = TestClient(create_app(Settings(), sessions))
    res = client.get("/api/market/discovery")
    assert res.status_code == 200
    body = res.json()
    assert body["discovery"]["coin"] == "xyz:CL"
    assert body["discovery"]["mark_px"] == "94.913"
    assert body["entries_allowed"] is False
    assert body["last_failure_after_discovery"] is None


def test_failure_after_discovery_is_surfaced(
    sessions: sessionmaker[Session], fake_info: FakeInfo
) -> None:
    assert run_discovery_once(Settings(), fake_info, sessions).ok
    # Backdate the success so the failure is strictly later.
    from autotrader.db.models import MarketMetadata

    with sessions.begin() as s:
        row = s.query(MarketMetadata).one()
        row.discovered_at = datetime.now(tz=UTC) - timedelta(hours=1)
    fake_info.responses["perpDexs"] = [None]
    outcome = run_discovery_once(Settings(), fake_info, sessions)
    assert not outcome.ok and outcome.error_code == "DEX_NOT_FOUND"
    body = TestClient(create_app(Settings(), sessions)).get("/api/market/discovery").json()
    assert body["stale"] is True
    assert body["last_failure_after_discovery"]["code"] == "DEX_NOT_FOUND"


def test_app_refuses_to_start_with_wallet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HL_PRIVATE_KEY", "0xabc")
    with pytest.raises(SafetyViolationError):
        create_app(Settings())


def test_worker_refuses_to_start_with_live_mode(
    monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo
) -> None:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    with pytest.raises(SafetyViolationError):
        run_discovery_once(Settings(), fake_info, None)
    assert fake_info.calls == []
