from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def perp_dexs() -> Any:
    return load_fixture("perpDexs_2026-11-03.json")


@pytest.fixture
def meta_xyz() -> Any:
    return load_fixture("metaAndAssetCtxs_xyz_2026-11-03.json")


class FakeInfo:
    """In-memory InfoSource returning recorded responses; records every payload."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def info(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        key = payload["type"]
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_info(perp_dexs: Any, meta_xyz: Any) -> FakeInfo:
    return FakeInfo({"perpDexs": perp_dexs, "metaAndAssetCtxs": meta_xyz})


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _block_external_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-live tests must never reach the internet (only localhost, e.g. PostgreSQL)."""
    if request.node.get_closest_marker("live"):
        return
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in _LOCAL_HOSTS:
            raise RuntimeError(f"external network access in a non-live test: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from autotrader.common.config import get_settings

    for var in ("TRADING_MODE", "AUTOTRADER_TRADING_MODE"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- Phase 1 helpers
from datetime import UTC, datetime  # noqa: E402

#: The moment the candle fixtures were recorded (the 15:30 30m bar was forming).
RECORDED_AT = datetime(2026, 11, 3, 15, 45, tzinfo=UTC)


@pytest.fixture
def snapshot_30m() -> Any:
    return load_fixture("candleSnapshot_xyzCL_30m_2026-11-03.json")


@pytest.fixture
def snapshot_1h() -> Any:
    return load_fixture("candleSnapshot_xyzCL_1h_2026-11-03.json")


@pytest.fixture
def ws_frames() -> list[str]:
    return (FIXTURES / "ws_frames_2026-11-03.jsonl").read_text().splitlines()


@pytest.fixture
def sqlite_sessions(tmp_path: Any) -> Any:
    """File-backed SQLite with one connection PER SESSION, like PostgreSQL.

    (An in-memory StaticPool shares ONE connection across threads: a session
    closing in one thread rolls back another thread's transaction - a test
    artifact that hid/created races the production database cannot have.)
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from autotrader.db.models import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class Clock:
    """Controllable clock for deterministic tests."""

    def __init__(self, now: datetime = RECORDED_AT) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(**kw)
