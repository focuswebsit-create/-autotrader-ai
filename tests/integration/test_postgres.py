"""Alembic migrations + DB guards on real PostgreSQL.

These tests DROP AND RECREATE all tables. They therefore use a dedicated variable,
``AUTOTRADER_TEST_DATABASE_URL``, and refuse any database whose name does not end
in ``_test`` (review finding M4), so they can never wipe a dev/prod database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from autotrader.common.config import Settings
from autotrader.db.repository import latest_discovery
from autotrader.worker.discovery_job import run_discovery_once
from tests.conftest import FakeInfo

DB_URL = os.environ.get("AUTOTRADER_TEST_DATABASE_URL", "")


def _is_safe_test_db(url: str) -> bool:
    if not url.startswith("postgresql"):
        return False
    return make_url(url).database is not None and str(make_url(url).database).endswith("_test")


pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        not _is_safe_test_db(DB_URL),
        reason="set AUTOTRADER_TEST_DATABASE_URL to a PostgreSQL database named *_test",
    ),
]


@pytest.fixture
def migrated() -> Iterator[str]:
    assert _is_safe_test_db(DB_URL)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", DB_URL)
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


def test_paper_only_row_cannot_be_renamed(migrated: str) -> None:
    """Review M2: renaming the key used to bypass the CHECK constraint."""
    engine = create_engine(migrated)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(
            text(
                "UPDATE system_config SET key='paper_only_disabled', value='false'::jsonb "
                "WHERE key='paper_only'"
            )
        )
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT value FROM system_config WHERE key='paper_only'")).scalar_one()
            is True
        )


@pytest.mark.parametrize("table", ["system_config", "audit_log"])
def test_guarded_tables_cannot_be_truncated(migrated: str, table: str) -> None:
    """Review M2: row-level triggers don't fire on TRUNCATE."""
    engine = create_engine(migrated)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(f"TRUNCATE {table}"))


def test_other_config_rows_still_editable(migrated: str) -> None:
    engine = create_engine(migrated)
    with engine.begin() as c:
        c.execute(
            text("UPDATE system_config SET value='\"2000\"'::jsonb WHERE key='initial_equity_usdc'")
        )


def test_orm_models_match_migrations(migrated: str) -> None:
    """No drift between SQLAlchemy models and Alembic head."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from autotrader.db.models import Base

    engine = create_engine(migrated)
    with engine.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    assert diff == []


def test_extreme_values_never_reach_the_database(
    migrated: str, perp_dexs: Any, meta_xyz: Any
) -> None:
    """Review M1: values the NUMERIC columns can't hold are rejected before insert."""
    import copy

    sessions = sessionmaker(bind=create_engine(migrated), expire_on_commit=False)
    meta = copy.deepcopy(meta_xyz)
    i = next(n for n, a in enumerate(meta[0]["universe"]) if a["name"] == "xyz:CL")
    meta[1][i]["markPx"] = "1e25"
    meta[1][i]["oraclePx"] = "1e25"
    fake = FakeInfo({"perpDexs": perp_dexs, "metaAndAssetCtxs": meta})
    outcome = run_discovery_once(Settings(), fake, sessions)
    assert not outcome.ok and outcome.error_code == "OUT_OF_RANGE"
    assert outcome.persisted is True
    with sessions() as s:
        assert latest_discovery(s, "xyz:CL") is None
