"""Paper-trader worker: the only process that changes PAPER accounts.

* Single instance per database: PostgreSQL advisory lock (a second worker exits).
* Every ``TICK_EVERY_S``: fetch the public ``l2Book`` (Info API, read-only, allow-listed),
  then run :func:`autotrader.paper.engine.tick` for each account in ONE transaction.
* On start it runs restart recovery (a RUNNING bot re-checks before trading again).
* Heartbeat ``paper-trader`` carries the live quote for the terminal.
* Recorded funding is refreshed every 10 minutes.

There is no code path to an exchange: ``InfoClient`` only allows POST /info.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session, sessionmaker

from autotrader.db.market_data import write_heartbeat
from autotrader.db.models import PaperAccount, ServiceHeartbeat
from autotrader.ingest.funding import sync_funding
from autotrader.paper import engine
from autotrader.paper.book import L2Book, parse_l2
from autotrader.paper.config import config_from_dict

log = logging.getLogger("autotrader.paper.worker")

SERVICE_NAME = "paper-trader"
TICK_EVERY_S = 3
FUNDING_EVERY = timedelta(minutes=10)
#: advisory lock: one paper engine per database ("PAPR")
PAPER_LOCK_KEY = 0x50415052


def fetch_book(source: Any, coin: str) -> tuple[L2Book | None, str | None]:
    try:
        raw = source.info({"type": "l2Book", "coin": coin})
    except Exception as exc:  # network / rate limit: no book this tick (fail-closed)
        return None, f"FETCH_FAILED:{type(exc).__name__}"
    book = parse_l2(raw, coin)
    return (book, None) if book else (None, "BOOK_INVALID")


def market_ctx(session: Session) -> dict[str, Any]:
    hb = session.get(ServiceHeartbeat, engine.COLLECTOR_SERVICE)
    if hb is None:
        return {}
    ctx = (hb.details or {}).get("market_ctx") or {}
    return dict(ctx) if isinstance(ctx, dict) else {}


def try_lock(conn: Connection) -> bool:
    if conn.dialect.name != "postgresql":
        return True
    return bool(
        conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": PAPER_LOCK_KEY}).scalar()
    )


def run_once(
    sessions: sessionmaker[Session],
    coin: str,
    source: Any,
    now: datetime,
    *,
    book: L2Book | None = None,
    book_error: str | None = None,
) -> dict[str, Any]:
    """One engine cycle for every account. ``book`` may be injected (tests)."""
    if book is None and book_error is None and source is not None:
        book, book_error = fetch_book(source, coin)
    with sessions() as s:
        ctx = market_ctx(s)
        ids = [a.id for a in engine.accounts(s, coin)]
    market = engine.Market(now=now, book=book, book_error=book_error, ctx=ctx)
    states: dict[str, str] = {}
    readiness: dict[str, Any] = {}
    for acc_id in ids:
        try:
            with sessions.begin() as s:
                acc = s.get(PaperAccount, acc_id)
                assert acc is not None
                engine.tick(s, acc, market)
                states[str(acc_id)] = engine.current_state(s, acc_id).state
                # the same checks START runs, published for the terminal's Start button
                cfg = config_from_dict(
                    {k: v for k, v in acc.config.items() if k != "history_anchor"}
                )
                checks = engine.preflight(s, acc, cfg, market)
                readiness[str(acc_id)] = {
                    "at": now.isoformat(),
                    "ok": all(c["ok"] for c in checks),
                    "checks": checks,
                }
        except Exception:
            # the transaction is rolled back; nothing half-written. Retry next tick.
            log.exception("paper tick failed", extra={"account": acc_id})
            states[str(acc_id)] = "ERROR"
            readiness[str(acc_id)] = {"at": now.isoformat(), "ok": False, "checks": [
                {"check": "ENGINE", "ok": False, "detail": "ENGINE_TICK_FAILED"}]}  # fmt: skip
    return {
        "states": states,
        "readiness": readiness,
        "book": None
        if book is None
        else {
            "time": book.time.isoformat(),
            "bid": str(book.best_bid),
            "ask": str(book.best_ask),
            "mid": str(book.mid),
            "spread_bps": str(round(book.spread_bps, 2)),
        },
        "book_error": book_error,
        "market_ctx": ctx,
    }


def recover(sessions: sessionmaker[Session], coin: str, source: Any, now: datetime) -> None:
    book, err = fetch_book(source, coin) if source is not None else (None, "NO_SOURCE")
    with sessions() as s:
        ctx = market_ctx(s)
        ids = [a.id for a in engine.accounts(s, coin)]
    market = engine.Market(now=now, book=book, book_error=err, ctx=ctx)
    for acc_id in ids:
        with sessions.begin() as s:
            acc = s.get(PaperAccount, acc_id)
            assert acc is not None
            cfg = config_from_dict({k: v for k, v in acc.config.items() if k != "history_anchor"})
            engine.recover_after_restart(s, acc, cfg, market)


def _heartbeat(
    sessions: sessionmaker[Session],
    state: str,
    started: datetime,
    now: datetime,
    details: dict[str, Any],
) -> None:
    try:
        with sessions.begin() as s:
            write_heartbeat(
                s,
                SERVICE_NAME,
                state=state,
                ws_state="NOT_APPLICABLE",
                started_at=started,
                last_heartbeat_at=now,
                details=details,
            )
    except Exception:
        log.exception("paper heartbeat failed")


def run_forever(
    sessions: sessionmaker[Session],
    lock_conn: Connection,
    coin: str,
    source: Any,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    stop: threading.Event | None = None,
) -> int:
    stop = stop or threading.Event()
    if not try_lock(lock_conn):
        log.error("another paper-trader holds the engine lock; exiting")
        return 5
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    started = clock()
    _heartbeat(sessions, "STARTING", started, started, {})
    recover(sessions, coin, source, started)
    last_funding: datetime | None = None
    while not stop.is_set():
        now = clock()
        if last_funding is None or now - last_funding >= FUNDING_EVERY:
            try:
                with sessions.begin() as s:
                    sync_funding(s, source, coin, start=now - timedelta(days=3), now=now)
                last_funding = now
            except Exception:
                log.exception("funding sync failed")
        details = run_once(sessions, coin, source, now)
        details["last_funding_sync"] = last_funding.isoformat() if last_funding else None
        _heartbeat(sessions, "RUNNING", started, now, details)
        stop.wait(TICK_EVERY_S)
    _heartbeat(sessions, "STOPPED", started, clock(), {})
    return 0
