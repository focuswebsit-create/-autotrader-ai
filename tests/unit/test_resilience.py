"""Regression tests: DB outages and unexpected errors must never crash or fake success."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from autotrader.api.app import create_app
from autotrader.common.config import Settings
from autotrader.common.safety import SafetyViolationError
from autotrader.worker import __main__ as worker_main
from autotrader.worker.discovery_job import run_discovery_once
from tests.conftest import FakeInfo

# Nothing listens on this port: every connection attempt fails fast.
DEAD_DB = "postgresql+psycopg://u:p@127.0.0.1:1/none?connect_timeout=1"


@pytest.fixture
def dead_sessions() -> sessionmaker[Session]:
    return sessionmaker(bind=create_engine(DEAD_DB))


def test_discovery_ok_but_db_down_is_reported_not_raised(
    fake_info: FakeInfo, dead_sessions: sessionmaker[Session]
) -> None:
    outcome = run_discovery_once(Settings(), fake_info, dead_sessions)
    assert outcome.ok is True
    assert outcome.persisted is False


def test_discovery_failed_and_db_down(
    fake_info: FakeInfo, dead_sessions: sessionmaker[Session]
) -> None:
    fake_info.responses["perpDexs"] = [None]
    outcome = run_discovery_once(Settings(), fake_info, dead_sessions)
    assert outcome.ok is False and outcome.error_code == "DEX_NOT_FOUND"
    assert outcome.persisted is False


class _Ctx:
    def __init__(self, src: Any) -> None:
        self.src = src

    def __call__(self, *a: Any, **k: Any) -> _Ctx:
        return self

    def __enter__(self) -> Any:
        return self.src

    def __exit__(self, *exc: object) -> None:
        return None


def test_worker_exit_code_3_when_db_down(
    monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo
) -> None:
    monkeypatch.setenv("AUTOTRADER_DATABASE_URL", DEAD_DB)
    monkeypatch.setattr(worker_main, "InfoClient", _Ctx(fake_info))
    assert worker_main.main([]) == worker_main.EXIT_NOT_PERSISTED


class _Exploding:
    def __init__(self) -> None:
        self.calls = 0

    def info(self, payload: dict[str, Any]) -> Any:
        self.calls += 1
        raise RuntimeError("unexpected bug")


def test_interval_loop_survives_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOTRADER_DATABASE_URL", raising=False)
    src = _Exploding()
    monkeypatch.setattr(worker_main, "InfoClient", _Ctx(src))
    monkeypatch.setattr(worker_main.time, "sleep", lambda s: None)
    code = worker_main.main(["--interval", "1"], max_iterations=3)
    assert code == worker_main.EXIT_UNEXPECTED
    assert src.calls == 3  # kept running after each failure


def test_interval_loop_stops_on_safety_violation(
    monkeypatch: pytest.MonkeyPatch, fake_info: FakeInfo
) -> None:
    monkeypatch.delenv("AUTOTRADER_DATABASE_URL", raising=False)
    monkeypatch.setattr(worker_main, "InfoClient", _Ctx(fake_info))
    monkeypatch.setenv("HL_PRIVATE_KEY", "0x1")
    with pytest.raises(SafetyViolationError):
        worker_main.main(["--interval", "1"], max_iterations=3)
    assert fake_info.calls == []


def test_negative_interval_rejected() -> None:
    with pytest.raises(SystemExit):
        worker_main.main(["--interval", "-5"])


def test_health_is_503_when_db_down(dead_sessions: sessionmaker[Session]) -> None:
    res = TestClient(create_app(Settings(), dead_sessions)).get("/health")
    assert res.status_code == 503
    body = res.json()
    assert body["database"] == "unavailable"
    assert body["trading_mode"] == "PAPER"


def test_discovery_endpoint_503_when_db_down(dead_sessions: sessionmaker[Session]) -> None:
    res = TestClient(create_app(Settings(), dead_sessions)).get("/api/market/discovery")
    assert res.status_code == 503
    assert res.json()["detail"] == "DATABASE_UNAVAILABLE"
