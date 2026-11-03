"""Alembic migrations + DB guards on real PostgreSQL. Requires AUTOTRADER_DATABASE_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from autotrader.common.config import Settings
from autotrader.db.repository import latest_discovery
from autotrader.worker.discovery_job import run_discovery_once
from tests.conftest import FakeInfo

DB_URL = os.environ.get("AUTOTRADER_DATABASE_URL", "")
pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not DB_URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


@pytest.fixture
def migrated() -> Iterator[str]:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield DB_URL
    command.downgrade(cfg, "base")


def test_upgrade_downgrade_roundtrip(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.connect() as c:
        tables = set(
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars()
        )
    assert {"market_metadata", "system_events", "audit_log", "system_config"} <= tables


def test_paper_only_row_cannot_be_disabled(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT value FROM system_config WHERE key='paper_only'")).scalar_one()
            is True
        )
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE system_config SET value='false'::jsonb WHERE key='paper_only'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM system_config WHERE key='paper_only'"))


def test_audit_log_append_only(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_log(actor,action,entity) VALUES ('t','a','e')"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE audit_log SET actor='x'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM audit_log"))


def test_worker_persists_discovery(migrated: str, fake_info: FakeInfo) -> None:
    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    assert run_discovery_once(Settings(), fake_info, sessions).ok
    with sessions() as s:
        row = latest_discovery(s, "xyz:CL")
        assert row is not None
        assert str(row.mark_px).startswith("94.913")
        assert row.status == "ACTIVE"
