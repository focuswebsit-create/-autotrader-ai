"""Forward-test lifecycle and the decision recorder (PAPER research only).

The recorder evaluates the frozen strategy on every 30m bar that CLOSED after the
test started, in chronological order, and appends one immutable row per bar:
decision (LONG/SHORT/none), reason, indicator rationale, a market snapshot, the
strategy version, parameter and code hashes and a digest of the input bars.

Guarantees (each enforced in code AND, where possible, in the database):

* only bars with ``open_time >= starts_at`` are ever recorded (DB trigger);
* only CLOSED bars are used (``closed_candles(as_of=now)``), ``evaluated_at >=
  bar_close_time`` (DB CHECK);
* a bar is decided once; rows are never updated or deleted (DB trigger);
* indicators always start at the frozen ``history_anchor``, so a later replay of
  the same bars reproduces each decision exactly (checked by the parity report);
* if the decision/fill code differs from the frozen ``code_hash`` the test is
  halted (INTEGRITY_HALT) and nothing more is recorded.

Nothing here can place an order: it only reads the database and writes journal rows.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autotrader.backtest.store import code_version as git_code_version
from autotrader.db.models import (
    BacktestRun,
    ForwardDecision,
    ForwardEvent,
    ForwardTest,
)
from autotrader.db.repository import latest_discovery
from autotrader.forward.freeze import (
    BAR,
    build_config,
    canonical_sha256,
    params_from_config,
    strategy_code_hash,
)
from autotrader.ingest.funding import load_funding
from autotrader.market.candles import Interval
from autotrader.strategy.base import Bar
from autotrader.strategy.strategies import build_strategy

log = logging.getLogger("autotrader.forward")

#: a decision made more than this after the bar closed is flagged as catch-up
LATE_AFTER = timedelta(minutes=10)
#: the latest closed bar older than this means the data feed is stale
STALE_AFTER = timedelta(minutes=45)
#: market snapshot from discovery is only attached if this fresh
DISCOVERY_MAX_AGE = timedelta(minutes=15)
#: bars hashed into ``input_digest``
DIGEST_BARS = 60
TERMINAL_EVENTS = ("STOPPED", "INTEGRITY_HALT")
FREEZE_LOCK_KEY = 0x46574446  # "FWDF"
#: how long to wait for the 1h bar that closes with a 30m bar before deciding anyway
H1_WAIT = timedelta(minutes=15)
#: hash of the code this process actually loaded (detects edits made under a running worker)
_LOADED_CODE_HASH = strategy_code_hash()


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- lifecycle


def test_state(session: Session, test_id: int) -> str:
    """ACTIVE, STOPPED or INTEGRITY_HALT (derived from the append-only event log)."""
    ev = session.scalar(
        select(ForwardEvent.event)
        .where(ForwardEvent.test_id == test_id, ForwardEvent.event.in_(TERMINAL_EVENTS))
        .order_by(ForwardEvent.id)
        .limit(1)
    )
    return ev or "ACTIVE"


def add_event(
    session: Session, test_id: int, event: str, detail: dict[str, Any], now: datetime
) -> None:
    session.add(ForwardEvent(test_id=test_id, event=event, detail=detail, occurred_at=now))
    session.flush()


def active_tests(session: Session, coin: str) -> list[ForwardTest]:
    tests = session.scalars(
        select(ForwardTest).where(ForwardTest.coin == coin).order_by(ForwardTest.id)
    )
    return [t for t in tests if test_state(session, t.id) == "ACTIVE"]


def reference_backtest(session: Session, coin: str, strategy_id: str, p_hash: str) -> Any:
    """Latest stored backtest of exactly these parameters (expectations to compare with)."""
    run = session.scalar(
        select(BacktestRun)
        .where(
            BacktestRun.coin == coin,
            BacktestRun.strategy_id == strategy_id,
            BacktestRun.params_hash == p_hash,
        )
        .order_by(BacktestRun.id.desc())
        .limit(1)
    )
    if run is None:
        return None
    s = run.summary
    days = float(s.get("days") or 0) or None
    return {
        "run_id": run.id,
        "verdict": run.verdict,
        "code_version": run.code_version,
        "data_fingerprint": run.data_fingerprint,
        "data_start": _utc(run.data_start).isoformat(),
        "data_end": _utc(run.data_end).isoformat(),
        "summary": {
            k: s.get(k)
            for k in (
                "trades",
                "win_rate",
                "profit_factor",
                "net_pnl",
                "net_return_pct",
                "max_drawdown_pct",
                "expectancy_r",
                "expectancy_r_ci95",
                "total_costs",
                "days",
            )
        },
        "trades_per_day": round(s["trades"] / days, 4) if days and s.get("trades") else None,
        "signals_per_day": round(
            run.report["primary"]["decision_counts"].get("SIGNAL", 0) / days, 4
        )
        if days
        else None,
    }


def create_test(
    session: Session,
    coin: str,
    strategy_id: str,
    *,
    now: datetime,
    code_version: str | None = None,
) -> ForwardTest:
    """Freeze and register a new forward test starting at the next 30m boundary.

    Refuses if an ACTIVE test for this strategy exists (one test at a time, so
    results can't be cherry-picked across parallel variants).
    """
    if session.get_bind().dialect.name == "postgresql":
        # serialise concurrent freezes: the one-active-test rule can't be a DB constraint
        # (activity is derived from the event log), so take a transaction-level lock
        from sqlalchemy import text

        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": FREEZE_LOCK_KEY})
    for t in active_tests(session, coin):
        if t.strategy_id == strategy_id:
            raise ValueError(f"an active forward test (#{t.id}) already exists for {strategy_id}")
    anchor = session.scalar(
        select(func.min(_candle_open_col())).where(*_candle_filter(coin, Interval.M30))
    )
    if anchor is None:
        raise ValueError("no closed 30m candles stored yet; run the collector/backfill first")
    anchor = _utc(anchor)
    probe = build_strategy(strategy_id)
    from autotrader.strategy.base import params_hash as ph

    p_hash = ph(strategy_id, probe.version, probe.params)
    cfg = build_config(
        strategy_id,
        now=now,
        history_anchor=anchor,
        code_version=code_version or git_code_version(),
        reference=reference_backtest(session, coin, strategy_id, p_hash),
    )
    test = ForwardTest(
        coin=coin,
        strategy_id=strategy_id,
        strategy_version=cfg["strategy"]["version"],
        params_hash=cfg["params_hash"],
        code_hash=cfg["code_hash"],
        code_version=cfg["code_version"],
        config=cfg,
        config_sha256=canonical_sha256(cfg),
        history_anchor=anchor,
        starts_at=datetime.fromisoformat(cfg["starts_at"]),
        created_at=now,
    )
    session.add(test)
    session.flush()
    add_event(
        session,
        test.id,
        "STARTED",
        {"starts_at": cfg["starts_at"], "config_sha256": test.config_sha256},
        now,
    )
    return test


def _candle_open_col() -> Any:
    from autotrader.db.models import CandleRow

    return CandleRow.open_time


def _candle_filter(coin: str, interval: Interval) -> list[Any]:
    from autotrader.db.models import CandleRow

    return [
        CandleRow.coin == coin,
        CandleRow.interval == interval.value,
        CandleRow.is_closed.is_(True),
    ]


# --------------------------------------------------------------------------- data


def load_bars(
    session: Session, coin: str, anchor: datetime, as_of: datetime
) -> tuple[list[Bar], list[Bar]]:
    """Closed 30m and 1h bars from the frozen anchor up to ``as_of``."""
    from autotrader.backtest.store import _bar
    from autotrader.db.market_data import closed_candles

    m30 = [_bar(r) for r in closed_candles(session, coin, Interval.M30, as_of=as_of, start=anchor)]
    # 1h history starts at the same anchor so trend EMAs are reproducible too
    h1 = [_bar(r) for r in closed_candles(session, coin, Interval.H1, as_of=as_of, start=anchor)]
    return m30, h1


def input_digest(m30: Sequence[Bar], i: int, h1: Sequence[Bar]) -> str:
    h = hashlib.sha256()
    t = m30[i].close_time
    for b in m30[max(0, i - DIGEST_BARS + 1) : i + 1]:
        h.update(
            f"m|{b.open_time.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode()
        )
    for b in [x for x in h1 if x.close_time <= t][-DIGEST_BARS:]:
        h.update(
            f"h|{b.open_time.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode()
        )
    return h.hexdigest()


# --------------------------------------------------------------------------- recording


@dataclass
class TickResult:
    test_id: int
    state: str
    recorded: int = 0
    signals: int = 0
    last_bar_close: datetime | None = None
    notes: list[str] = field(default_factory=list)


def tick(session: Session, test: ForwardTest, *, now: datetime) -> TickResult:
    """Record decisions for every newly closed bar of one test (idempotent)."""
    res = TickResult(test.id, test_state(session, test.id))
    if res.state != "ACTIVE":
        return res
    code_hash = strategy_code_hash()
    if code_hash != test.code_hash or test.code_hash != _LOADED_CODE_HASH:
        add_event(
            session,
            test.id,
            "INTEGRITY_HALT",
            {
                "reason": "CODE_CHANGED",
                "frozen": test.code_hash,
                "on_disk": code_hash,
                "loaded": _LOADED_CODE_HASH,
            },
            now,
        )
        res.state = "INTEGRITY_HALT"
        res.notes.append("strategy/engine code changed since freeze - test halted")
        log.error("forward test halted: code changed", extra={"test_id": test.id})
        return res
    cfg = test.config
    params = params_from_config(cfg)
    starts_at = _utc(test.starts_at)
    m30, h1 = load_bars(session, test.coin, _utc(test.history_anchor), now)
    if not m30:
        res.notes.append("no closed bars")
        return res
    last_done = session.scalar(
        select(func.max(ForwardDecision.bar_open_time)).where(ForwardDecision.test_id == test.id)
    )
    last_done = _utc(last_done) if last_done else None
    todo = [
        i
        for i, b in enumerate(m30)
        if b.open_time >= starts_at and (last_done is None or b.open_time > last_done)
    ]
    # A 30m bar closing on the hour needs the 1h bar that closes at the same instant;
    # the collector commits the two intervals separately. Deciding before the 1h bar
    # is stored would differ from any later replay, so wait for it (up to H1_WAIT; after
    # that the 1h bar is treated as genuinely missing and the gap is recorded).
    h1_last = h1[-1].close_time if h1 else None
    ready: list[int] = []
    for i in todo:
        t = m30[i].close_time
        need = t.replace(minute=0, second=0, microsecond=0)
        if h1_last is not None and h1_last >= need:
            ready.append(i)
        elif now - t >= H1_WAIT:
            _event_once(session, test.id, "DATA_GAP", {"h1_missing_at": need.isoformat()}, now)
            ready.append(i)
        else:
            res.notes.append(f"waiting for the 1h bar closing {need.isoformat()}")
            break
    todo = ready
    newest = m30[-1].close_time
    res.last_bar_close = newest
    if now - newest > STALE_AFTER and now >= starts_at + STALE_AFTER:
        _event_once(session, test.id, "DATA_STALE", {"last_bar_close": newest.isoformat()}, now)
        res.notes.append("market data stale (collector down?)")
    if not todo:
        return res
    # missing bars inside the test period are recorded as a gap, never filled
    prev = last_done + BAR if last_done else starts_at
    for i in todo:
        if m30[i].open_time > prev:
            missing = int((m30[i].open_time - prev) / BAR)
            add_event(
                session,
                test.id,
                "DATA_GAP",
                {"from": prev.isoformat(), "to": m30[i].open_time.isoformat(), "bars": missing},
                now,
            )
        prev = m30[i].close_time
    strat = build_strategy(cfg["strategy"]["id"], params)
    prepared = strat.prepare(m30, h1)
    funding = load_funding(session, test.coin, as_of=now)
    disc = latest_discovery(session, test.coin)
    for i in todo:
        bar = m30[i]
        d = prepared.decide(i)
        late = now - bar.close_time > LATE_AFTER
        session.add(
            ForwardDecision(
                test_id=test.id,
                bar_open_time=bar.open_time,
                bar_close_time=bar.close_time,
                evaluated_at=now,
                late=late,
                side=d.side.value if d.side else None,
                reason=d.reason,
                atr=d.atr,
                rationale=d.rationale,
                market=_market_snapshot(bar, funding, disc, now, late),
                strategy_version=cfg["strategy"]["version"],
                params_hash=cfg["params_hash"],
                code_hash=code_hash,
                input_digest=input_digest(m30, i, h1),
            )
        )
        res.recorded += 1
        res.signals += int(d.side is not None)
    session.flush()
    return res


def _event_once(
    session: Session, test_id: int, event: str, detail: dict[str, Any], now: datetime
) -> None:
    last = session.scalar(
        select(ForwardEvent)
        .where(ForwardEvent.test_id == test_id, ForwardEvent.event == event)
        .order_by(ForwardEvent.id.desc())
        .limit(1)
    )
    if last is not None and last.detail == detail:
        return
    add_event(session, test_id, event, detail, now)


def _market_snapshot(
    bar: Bar, funding: dict[datetime, Any], disc: Any, now: datetime, late: bool
) -> dict[str, Any]:
    hour = bar.close_time.replace(minute=0, second=0, microsecond=0)
    rates = [r for t, r in funding.items() if t <= bar.close_time]
    snap: dict[str, Any] = {
        "bar": {
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "trades": bar.trades,
        },
        "funding_rate_last": str(rates[-1]) if rates else None,
        "funding_hour": hour.isoformat(),
        "mark_px": None,
        "oracle_px": None,
        "oracle_mark_divergence_bps": None,
    }
    if disc is not None and not late:
        at = _utc(disc.discovered_at)
        if abs(at - now) <= DISCOVERY_MAX_AGE:
            snap |= {
                "mark_px": str(disc.mark_px),
                "oracle_px": str(disc.oracle_px),
                "oracle_mark_divergence_bps": str(disc.oracle_mark_divergence_bps),
                "discovered_at": at.isoformat(),
            }
    return snap


def stop_test(session: Session, test_id: int, *, reason: str, now: datetime) -> None:
    if test_state(session, test_id) != "ACTIVE":
        raise ValueError("test is not active")
    add_event(session, test_id, "STOPPED", {"reason": reason[:500]}, now)
