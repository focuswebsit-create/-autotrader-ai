"""Live market-data collector (asyncio).

Responsibilities:
* Keep a guarded, read-only WS connection to Hyperliquid, subscribed to the
  candle channels and ``activeAssetCtx`` (mark / oracle) for one market.
* Detect staleness (no frame for ``stale_after``) and disconnects; reconnect
  with exponential backoff + jitter.
* After every (re)connect, and periodically, re-sync from REST: REST is the
  source of truth that closes bars and fills anything the stream missed.
* Write a heartbeat row so the API/UI can show the real collector state.

No network I/O happens here directly: the WS goes through
:func:`autotrader.common.ws.connect` and REST through ``InfoClient`` - both
allow-listed. Dependencies are injected so every failure path is testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.safety import SafetyViolationError
from autotrader.common.ws import WsConnection
from autotrader.db.market_data import (
    record_findings,
    record_rejections,
    upsert_candles,
    write_heartbeat,
)
from autotrader.db.models import CandleRow
from autotrader.ingest.rest import InfoSource, needs_finalize, sync_since
from autotrader.market.candles import Candle, Interval, Rejected, parse_candle

log = logging.getLogger("autotrader.collector")

SERVICE_NAME = "market-data-collector"


@dataclass
class CollectorConfig:
    coin: str
    intervals: Sequence[Interval] = (Interval.M30, Interval.H1)
    #: No WS frame for this long -> connection considered stale and recycled.
    stale_after: timedelta = timedelta(seconds=45)
    ping_every: timedelta = timedelta(seconds=20)
    sync_every: timedelta = timedelta(seconds=30)
    heartbeat_every: timedelta = timedelta(seconds=10)
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    #: How long a single recv() waits before re-checking staleness / ping.
    recv_poll_s: float = 1.0


@dataclass
class CollectorState:
    ws_state: str = "DISCONNECTED"
    started_at: datetime | None = None
    last_ws_message_at: datetime | None = None
    last_candle_update_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_sync_ok: bool | None = None
    reconnects: int = 0
    consecutive_failures: int = 0
    messages: int = 0
    ignored_out_of_order: int = 0
    rejected: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None
    #: The last error the collector recovered from (kept for diagnostics).
    recovered_error: str | None = None
    recovered_at: datetime | None = None
    market_ctx: dict[str, Any] = field(default_factory=dict)


Connector = Callable[[], Awaitable[WsConnection]]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class LiveCollector:
    def __init__(
        self,
        config: CollectorConfig,
        *,
        sessions: sessionmaker[Session],
        info: InfoSource,
        connect: Connector,
        clock: Clock = utcnow,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = config
        self.sessions = sessions
        self.info = info
        self.connect = connect
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()  # noqa: S311 - jitter, not crypto
        self.state = CollectorState()
        self._conn: WsConnection | None = None
        # REST sync and WS handling run in worker threads.
        # * _write_lock: held ONLY around candle DB writes (never network I/O), so
        #   two writers never race on the same (coin, interval, open_time) and a
        #   slow REST call can't stall frame handling (review finding M2).
        # * _sync_lock: serialises whole REST syncs (no overlapping syncs).
        self._write_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        #: Set on shutdown: no new writes start after it (review finding M4).
        self._closed = False
        self._session_connected = False
        self._inflight: set[asyncio.Task[bool]] = set()
        self._stop: asyncio.Event | None = None
        #: Set when a background task hit a safety violation; run() re-raises it.
        self._fatal: SafetyViolationError | None = None

    # ------------------------------------------------------------------ helpers

    def _set_error(self, message: str | None) -> None:
        self.state.last_error = message
        self.state.last_error_at = self.clock()

    def _recovered(self) -> None:
        """Called after a successful (re)connect / sync: the error is history now."""
        if self.state.last_error is not None:
            self.state.recovered_error = self.state.last_error
            self.state.recovered_at = self.clock()
            self.state.last_error = None
            self.state.last_error_at = None

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        cap = min(self.cfg.backoff_max_s, self.cfg.backoff_initial_s * (2 ** max(attempt - 1, 0)))
        return self.rng.uniform(cap / 2, cap)

    def subscriptions(self) -> list[dict[str, Any]]:
        subs: list[dict[str, Any]] = [
            {"type": "candle", "coin": self.cfg.coin, "interval": iv.value}
            for iv in self.cfg.intervals
        ]
        subs.append({"type": "activeAssetCtx", "coin": self.cfg.coin})
        return subs

    def last_closed_open_time(self, interval: Interval) -> datetime | None:
        with self.sessions() as s:
            value = s.scalar(
                select(func.max(CandleRow.open_time)).where(
                    CandleRow.coin == self.cfg.coin,
                    CandleRow.interval == interval.value,
                    CandleRow.is_closed.is_(True),
                )
            )
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    # ------------------------------------------------------------------ REST sync

    def sync_rest(self, *, force: bool, kind: str = "gap_fill") -> bool:
        """Finalize closed bars and fill gaps from REST. Returns overall success."""
        if self._closed:
            return False
        with self._sync_lock:
            if self._closed:
                return False
            return self._sync_rest_locked(force=force, kind=kind)

    def _drain(self) -> None:
        """Block until no sync or candle write is in progress; refuse new ones.

        Called on shutdown before the heartbeat says STOPPED and before the
        caller releases the single-writer lock.
        """
        self._closed = True
        with self._sync_lock, self._write_lock:
            pass

    def _spawn_sync(self, *, force: bool, kind: str) -> asyncio.Task[bool]:
        task = asyncio.create_task(asyncio.to_thread(self.sync_rest, force=force, kind=kind))
        self._inflight.add(task)
        task.add_done_callback(self._sync_done)
        return task

    def _sync_done(self, task: asyncio.Task[bool]) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, SafetyViolationError):
            # Never swallow a safety violation from a background sync: stop.
            self._fatal = exc
            if self._stop is not None:
                self._stop.set()
        elif exc is not None:
            self._set_error(f"sync: {type(exc).__name__}")
            log.error("background sync failed", extra={"error": type(exc).__name__})

    def _sync_rest_locked(self, *, force: bool, kind: str) -> bool:
        ok = True
        for iv in self.cfg.intervals:
            try:
                last = self.last_closed_open_time(iv)
                if not force and not needs_finalize(last, iv, self.clock()):
                    continue
                results = sync_since(
                    self.sessions,
                    self.info,
                    coin=self.cfg.coin,
                    interval=iv,
                    last_closed_open_time=last,
                    kind=kind,
                    clock=self.clock,
                    write_lock=self._write_lock,
                )
                ok = ok and all(r.ok for r in results)
                if results and not results[-1].ok:
                    self._set_error(results[-1].error)
            except SQLAlchemyError as exc:
                ok = False
                self._set_error(f"DATABASE: {type(exc).__name__}")
                log.error("rest sync failed: database", extra={"interval": iv.value})
        self.state.last_sync_at = self.clock()
        self.state.last_sync_ok = ok
        return ok

    # ------------------------------------------------------------------ messages

    def handle_message(self, raw: str | bytes) -> None:
        now = self.clock()
        self.state.last_ws_message_at = now
        self.state.messages += 1
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._set_error("WS: non-JSON frame")
            return
        if not isinstance(msg, dict):
            return
        channel, data = msg.get("channel"), msg.get("data")
        if channel == "candle":
            for item in data if isinstance(data, list) else [data]:
                self._handle_candle(item, now)
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            ctx = data.get("ctx")
            if data.get("coin") == self.cfg.coin and isinstance(ctx, dict):
                self.state.market_ctx = {
                    k: ctx.get(k) for k in ("markPx", "oraclePx", "midPx", "funding")
                } | {"received_at": now.isoformat()}
        elif channel == "error":
            self._set_error(f"WS error: {str(data)[:200]}")
            log.warning("ws error frame", extra={"data": str(data)[:200]})
        # "pong" and "subscriptionResponse" need no action beyond the timestamp.

    def _handle_candle(self, item: Any, now: datetime) -> None:
        interval_raw = item.get("i") if isinstance(item, dict) else None
        try:
            interval = Interval(str(interval_raw))
        except ValueError:
            self.state.rejected += 1
            return
        if interval not in self.cfg.intervals:
            return
        parsed = parse_candle(item, coin=self.cfg.coin, interval=interval)
        with self._write_lock:
            if self._closed:
                return
            self._write_candle(parsed, interval, now)

    def _write_candle(self, parsed: Candle | Rejected, interval: Interval, now: datetime) -> None:
        with self.sessions.begin() as s:
            if isinstance(parsed, Rejected):
                self.state.rejected += 1
                record_rejections(s, self.cfg.coin, interval, [parsed], now)
                return
            assert isinstance(parsed, Candle)
            stats = upsert_candles(s, [parsed], source="ws", now=now)
            record_findings(s, self.cfg.coin, interval, stats.findings, now)
        self.state.ignored_out_of_order += stats.ignored_out_of_order
        if stats.inserted or stats.updated:
            self.state.last_candle_update_at = now

    # ------------------------------------------------------------------ heartbeat

    def health_state(self) -> str:
        if self.state.ws_state == "CONNECTED" and self.state.last_sync_ok is not False:
            return "RUNNING"
        return "DEGRADED"

    def write_heartbeat(self, *, stopping: bool = False) -> None:
        now = self.clock()
        try:
            with self.sessions.begin() as s:
                write_heartbeat(
                    s,
                    SERVICE_NAME,
                    state="STOPPED" if stopping else self.health_state(),
                    ws_state="DISCONNECTED" if stopping else self.state.ws_state,
                    started_at=self.state.started_at or now,
                    last_heartbeat_at=now,
                    last_ws_message_at=self.state.last_ws_message_at,
                    last_candle_update_at=self.state.last_candle_update_at,
                    reconnects=self.state.reconnects,
                    details={
                        "coin": self.cfg.coin,
                        "intervals": [i.value for i in self.cfg.intervals],
                        "messages": self.state.messages,
                        "ignored_out_of_order": self.state.ignored_out_of_order,
                        "rejected": self.state.rejected,
                        "last_sync_at": (
                            self.state.last_sync_at.isoformat() if self.state.last_sync_at else None
                        ),
                        "last_sync_ok": self.state.last_sync_ok,
                        "last_error": self.state.last_error,
                        "last_error_at": _iso(self.state.last_error_at),
                        "recovered_error": self.state.recovered_error,
                        "recovered_at": _iso(self.state.recovered_at),
                        "market_ctx": self.state.market_ctx,
                    },
                )
        except SQLAlchemyError as exc:
            log.error("heartbeat not written: database", extra={"error": type(exc).__name__})

    # ------------------------------------------------------------------ main loop

    async def run(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.state.started_at = self.clock()
        self.state.ws_state = "DISCONNECTED"
        await asyncio.to_thread(self.write_heartbeat)
        # Initial history: request everything the API has, per interval.
        await asyncio.to_thread(self.sync_rest, force=True, kind="gap_fill")
        background = [
            asyncio.create_task(self._heartbeat_loop(stop)),
            asyncio.create_task(self._sync_loop(stop)),
        ]
        attempt = 0
        try:
            while not stop.is_set():
                self._session_connected = False
                try:
                    await self._session(stop)
                except (asyncio.CancelledError, SafetyViolationError):
                    # A safety violation is a bug, never a transient error: stop.
                    raise
                except Exception as exc:
                    # Any connection/protocol/DB problem: log, back off, reconnect.
                    self._set_error(f"{type(exc).__name__}: {str(exc)[:200]}")
                    log.warning("ws session ended", extra={"error": self.state.last_error})
                if stop.is_set():
                    break
                if self._session_connected:
                    # The last session worked: start backoff from scratch (finding M1).
                    attempt = 0
                attempt += 1
                self.state.consecutive_failures = attempt
                self.state.reconnects += 1
                self.state.ws_state = "DISCONNECTED"
                delay = self.backoff_delay(attempt)
                log.info("reconnecting", extra={"attempt": attempt, "delay_s": round(delay, 2)})
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            # Cancelling a task does not stop its worker thread: wait until no
            # sync/write is running and refuse new ones, THEN report STOPPED.
            await asyncio.to_thread(self._drain)
            await asyncio.gather(*list(self._inflight), return_exceptions=True)
            await asyncio.to_thread(self.write_heartbeat, stopping=True)
        if self._fatal is not None:
            raise self._fatal

    async def _session(self, stop: asyncio.Event) -> None:
        self.state.ws_state = "CONNECTING"
        conn = await self.connect()
        self._conn = conn
        try:
            for sub in self.subscriptions():
                await conn.send(json.dumps({"method": "subscribe", "subscription": sub}))
            self.state.ws_state = "CONNECTED"
            self.state.last_ws_message_at = self.clock()
            self._session_connected = True
            self._recovered()
            log.info("ws connected", extra={"subscriptions": len(self.subscriptions())})
            # Fill anything missed while disconnected (REST is the source of truth).
            # Runs in the background so frames keep being read meanwhile (M2).
            self._spawn_sync(force=True, kind="gap_fill")
            last_ping = self.clock()
            while not stop.is_set():
                now = self.clock()
                if now - last_ping >= self.cfg.ping_every:
                    await conn.send(json.dumps({"method": "ping"}))
                    last_ping = now
                try:
                    frame = await asyncio.wait_for(conn.recv(), timeout=self.cfg.recv_poll_s)
                except TimeoutError:
                    frame = None
                if frame is not None:
                    await asyncio.to_thread(self.handle_message, frame)
                last_msg = self.state.last_ws_message_at or now
                if self.clock() - last_msg > self.cfg.stale_after:
                    self.state.ws_state = "STALE"
                    raise ConnectionError(f"no WS data for {self.cfg.stale_after}")
        finally:
            self._conn = None
            with contextlib.suppress(Exception):
                await conn.close()

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.to_thread(self.write_heartbeat)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.cfg.heartbeat_every.total_seconds())

    async def _sync_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.cfg.sync_every.total_seconds())
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(self.sync_rest, force=False, kind="finalize")
            except SafetyViolationError as exc:
                self._fatal = exc
                stop.set()
                return
            except Exception as exc:
                self._set_error(f"sync: {type(exc).__name__}")
                log.exception("periodic sync failed")
