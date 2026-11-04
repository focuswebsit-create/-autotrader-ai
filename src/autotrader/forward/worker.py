"""Forward-test worker: records decisions for every ACTIVE test as bars close.

Runs next to the market-data collector (which writes the closed candles). It also
refreshes recorded funding from the public Info API about once an hour. It never
creates, changes or stops a test, and it has no code path that can place an order.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.forward.recorder import TickResult, active_tests, tick
from autotrader.ingest.funding import sync_funding

log = logging.getLogger("autotrader.forward.worker")

TICK_EVERY_S = 30
FUNDING_EVERY = timedelta(minutes=60)


def tick_all(sessions: sessionmaker[Session], coin: str, now: datetime) -> list[TickResult]:
    out: list[TickResult] = []
    with sessions() as s:
        ids = [t.id for t in active_tests(s, coin)]
    for test_id in ids:
        # one transaction per test: a failure in one never blocks the others
        with sessions.begin() as s:
            from autotrader.db.models import ForwardTest

            test = s.get(ForwardTest, test_id)
            if test is not None:
                out.append(tick(s, test, now=now))
    return out


def run_forever(
    sessions: sessionmaker[Session],
    coin: str,
    info_source: Any | None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    stop: threading.Event | None = None,
) -> None:
    stop = stop or threading.Event()

    def _sig(*_: Any) -> None:
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
    last_funding: datetime | None = None
    while not stop.is_set():
        now = clock()
        if info_source is not None and (
            last_funding is None or now - last_funding >= FUNDING_EVERY
        ):
            try:
                with sessions.begin() as s:
                    sync_funding(s, info_source, coin, start=now - timedelta(days=3), now=now)
                last_funding = now
            except Exception:
                log.exception("funding sync failed")
        try:
            for r in tick_all(sessions, coin, now):
                if r.recorded or r.notes:
                    log.info(
                        "forward tick",
                        extra={
                            "test_id": r.test_id,
                            "state": r.state,
                            "recorded": r.recorded,
                            "signals": r.signals,
                            "notes": r.notes,
                        },
                    )
        except SQLAlchemyError:
            log.exception("forward tick failed (database)")
        stop.wait(TICK_EVERY_S)
