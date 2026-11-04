"""Market-data collector service: ``python -m autotrader.collector_main``.

Startup sequence (fail closed at every step):
1. PAPER-only safety checks (env, mode).
2. Database configured and reachable; single-writer advisory lock acquired.
3. Market discovery of the target coin succeeds (runtime metadata, no hard-coding).
4. REST backfill of all available history, then the live WS loop.

Exit codes: 0 clean stop, 2 discovery failed, 3 no/unreachable database,
5 another collector already holds the lock, 6 the lock was lost while running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from autotrader.common import ws
from autotrader.common.config import get_settings
from autotrader.common.http import InfoClient
from autotrader.common.logging import configure_logging
from autotrader.common.safety import assert_paper_mode, check_environment
from autotrader.db.market_data import (
    lock_still_held,
    open_lock_connection,
    release_collector_lock,
    try_collector_lock,
)
from autotrader.db.session import session_factory
from autotrader.ingest.live import CollectorConfig, LiveCollector
from autotrader.worker.discovery_job import run_discovery_once

log = logging.getLogger("autotrader.collector")

DISCOVERY_EVERY_S = 300
LOCK_CHECK_EVERY_S = 10


async def _discovery_loop(stop: asyncio.Event, run: object) -> None:
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), DISCOVERY_EVERY_S)
        if stop.is_set():
            break
        try:
            await asyncio.to_thread(run)  # type: ignore[arg-type]
        except Exception:
            log.exception("periodic discovery failed")


async def _lock_guard(stop: asyncio.Event, lost: asyncio.Event, conn: Connection) -> None:
    """Stop the collector if the single-writer lock was lost (e.g. DB restart).

    The process then exits (code 6) and the restart policy starts a fresh
    instance that must re-acquire the lock before writing again.
    """
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), LOCK_CHECK_EVERY_S)
        if stop.is_set():
            return
        if not await asyncio.to_thread(lock_still_held, conn):
            log.critical("single-writer lock lost - stopping collector")
            lost.set()
            stop.set()
            return


async def amain() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    check_environment()
    assert_paper_mode()
    if not settings.database_url:
        log.error("AUTOTRADER_DATABASE_URL is required for the collector")
        return 3
    sessions = session_factory(settings.database_url)

    try:
        lock_conn = open_lock_connection(settings.database_url)
        if not try_collector_lock(lock_conn):
            release_collector_lock(lock_conn)
            log.error("another collector is running (advisory lock held) - exiting")
            return 5
    except SQLAlchemyError:
        log.exception("database unreachable at startup")
        return 3

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        with InfoClient() as client:
            outcome = await asyncio.to_thread(run_discovery_once, settings, client, sessions)
            if not outcome.ok:
                log.error("market discovery failed - collector not started (fail-closed)")
                return 2
            collector = LiveCollector(
                CollectorConfig(coin=settings.market_coin),
                sessions=sessions,
                info=client,
                connect=ws.connect,
            )
            disc = asyncio.create_task(
                _discovery_loop(stop, lambda: run_discovery_once(settings, client, sessions))
            )
            lost = asyncio.Event()
            guard = asyncio.create_task(_lock_guard(stop, lost, lock_conn))
            try:
                await collector.run(stop)
            finally:
                for task in (disc, guard):
                    task.cancel()
                await asyncio.gather(disc, guard, return_exceptions=True)
            if lost.is_set():
                return 6
    finally:
        release_collector_lock(lock_conn)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
