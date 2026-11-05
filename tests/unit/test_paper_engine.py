"""Phase 3 PAPER execution engine: lifecycle, fills from the live book, exits, funding,
risk limits, kill switch, restart, idempotency, accounting (SQLite; PostgreSQL in
tests/integration/test_postgres.py)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from autotrader.db.models import (
    Base,
    FundingRate,
    PaperDecision,
    PaperOrder,
    PaperPosition,
    PaperPositionClose,
    PaperRiskEvent,
)
from autotrader.paper import book as bk
from autotrader.paper import engine
from autotrader.paper.config import PaperConfig
from autotrader.strategy.base import Side
from tests.unit.paper_helpers import (
    COIN,
    STEP,
    D,
    ScriptedStrategy,
    add_bars,
    book,
    collector_ok,
    command,
    funding,
    tick,
    utc,
)

#: Tuesday 2026-10-13 14:00 UTC = 10:00 New York (CME open; no holiday, no roll window)
T0 = utc(2026, 10, 13, 14, 0)
SIG_BAR = T0  # the bar [T0, T0+30m) carries the scripted signal
L, S = Side.LONG, Side.SHORT


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    eng = create_engine(f"sqlite:///{tmp_path / 'p.db'}", poolclass=NullPool)
    Base.metadata.create_all(eng)
    sessions = sessionmaker(bind=eng, expire_on_commit=False)
    with sessions.begin() as s:
        add_bars(s, T0 - 200 * STEP, 200)
        add_bars(s, T0 - timedelta(hours=100), 100, interval="1h")
        for h in range(1, 6):
            funding(s, T0 - timedelta(hours=h))
        collector_ok(s, T0)
        acc = engine.create_account(s, name="demo", coin=COIN, now=T0)
        acc_id = acc.id
    script: dict[Any, Side] = {}
    monkeypatch.setattr(engine, "build_strategy", lambda _sid: ScriptedStrategy(script))
    return sessions, acc_id, script


def start(sessions: sessionmaker[Session], acc_id: int, now: Any = T0) -> None:
    command(sessions, acc_id, "START", now, f"start-{now.timestamp()}")
    tick(sessions, acc_id, now, book(now, "80.00", "80.02"))


def close_bar(
    sessions: sessionmaker[Session], open_time: Any, px: str = "80", hb_at: Any = None
) -> None:
    with sessions.begin() as s:
        add_bars(s, open_time, 1, px)
        if open_time.minute == 30:  # the 1h bar closing with it
            add_bars(s, open_time - timedelta(minutes=30), 1, px, interval="1h")
        collector_ok(s, hb_at or open_time + STEP)
        hour = (open_time + STEP).replace(minute=0, second=0, microsecond=0)
        if s.scalar(select(FundingRate).where(FundingRate.hour == hour)) is None:
            funding(s, hour)  # the funding feed keeps up with the bars


def state(sessions: sessionmaker[Session], acc_id: int) -> str:
    with sessions() as s:
        return engine.current_state(s, acc_id).state


def count(sessions: sessionmaker[Session], model: Any) -> int:
    with sessions() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def reconcile(sessions: sessionmaker[Session], acc_id: int) -> None:
    with sessions() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        ok, detail = engine.ledger_reconciles(s, acc)
        assert ok, detail


# --------------------------------------------------------------------------- lifecycle


def test_new_account_is_stopped_with_1000_usdc(env: Any) -> None:
    sessions, acc_id, _ = env
    with sessions() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        assert acc.initial_equity == D(1000) and engine.cash(s, acc_id) == D(1000)
        assert acc.config["paper_only"] is True
        assert acc.config["risk"]["risk_per_trade_pct"] == "0.25"
        assert acc.config["risk"]["daily_loss_limit_pct"] == "1.0"
        assert acc.config["risk"]["drawdown_halt_pct"] == "5.0"
    assert state(sessions, acc_id) == "STOPPED"


def test_start_runs_preflight_and_refuses_when_collector_is_down(env: Any) -> None:
    sessions, acc_id, _ = env
    later = T0 + timedelta(minutes=5)  # collector heartbeat is from T0: 5 min old -> down
    command(sessions, acc_id, "START", later, "start-1")
    tick(sessions, acc_id, later, book(later, "80.00", "80.02"))
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
    assert ev.state == "STOPPED" and ev.reason == "PREFLIGHT_FAILED"
    assert "COLLECTOR" in ev.detail["failed"]


def test_start_succeeds_with_healthy_inputs(env: Any) -> None:
    sessions, acc_id, _ = env
    start(sessions, acc_id)
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "RUNNING" and ev.reason == "STARTED"
        assert all(c["ok"] for c in ev.detail["checks"])
        assert engine.session_start(s, acc_id) == T0


# --------------------------------------------------------------------------- LONG round trip


def _open_long(env: Any) -> tuple[Any, int, Any]:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR)
    now = SIG_BAR + STEP + timedelta(seconds=20)
    tick(sessions, acc_id, now, book(now, "80.00", "80.02"))
    return sessions, acc_id, now


def test_long_entry_fills_from_live_asks_with_costs(env: Any) -> None:
    sessions, acc_id, _now = _open_long(env)
    with sessions() as s:
        pos = engine.open_position(s, acc_id)
        assert pos is not None and pos.side == "LONG"
        order = s.get(PaperOrder, pos.entry_order_id)
        # walked the asks (best 80.02) + 2 bps buffer, rounded up to the tick
        assert order.reference_price == D("80.02")
        assert pos.entry_price == D("80.037")  # 80.02 * 1.0002 = 80.036004 -> up
        assert pos.stop == D("78.537") and pos.target == D("83.037")
        assert order.fee == (D("0.00009") * pos.entry_price * pos.size).quantize(D("1e-8"))
        assert pos.planned_loss <= pos.risk_budget == D("2.5")
        assert order.status == "FILLED" and order.client_order_id.startswith("entry:")
        dec = s.scalar(select(PaperDecision))
        assert dec.outcome == "ORDER" and dec.evaluated_at >= dec.bar_close_time
    reconcile(sessions, acc_id)


def test_stop_exit_walks_the_bids_and_books_net_pnl(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    later = now + timedelta(seconds=60)
    tick(sessions, acc_id, later, book(later, "78.50", "78.52"))
    with sessions() as s:
        c = s.scalar(select(PaperPositionClose))
        pos = s.get(PaperPosition, c.position_id)
        assert c.reason == "STOP" and c.exit_price == D("78.484")  # 78.50*(1-0.0002) down
        assert c.gross_pnl == ((c.exit_price - pos.entry_price) * pos.size).quantize(D("1e-8"))
        assert c.net_pnl == c.gross_pnl - c.fees - c.funding
        assert engine.open_position(s, acc_id) is None
        assert engine.cash(s, acc_id) == D(1000) + c.net_pnl
    reconcile(sessions, acc_id)


def test_take_profit_fills_at_target_only_with_depth(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=60)
    # bid at target but tiny size: no fill (a resting limit can't fill more than shown)
    tick(sessions, acc_id, t, book(t, "83.04", "83.06", size="0.001", levels=1))
    assert count(sessions, PaperPositionClose) == 0
    t2 = t + timedelta(seconds=5)
    tick(sessions, acc_id, t2, book(t2, "83.10", "83.12"))
    with sessions() as s:
        c = s.scalar(select(PaperPositionClose))
        assert c.reason == "TAKE_PROFIT" and c.exit_price == D("83.037") and c.net_pnl > 0
    reconcile(sessions, acc_id)


def test_short_round_trip(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    script[SIG_BAR] = S
    close_bar(sessions, SIG_BAR)
    now = SIG_BAR + STEP + timedelta(seconds=20)
    tick(sessions, acc_id, now, book(now, "80.00", "80.02"))
    with sessions() as s:
        pos = engine.open_position(s, acc_id)
        assert pos.side == "SHORT" and pos.entry_price == D("79.984")  # 80.00*(1-0.0002) down
        assert pos.stop > pos.entry_price > pos.target
    t = now + timedelta(seconds=30)
    tick(sessions, acc_id, t, book(t, "76.90", "76.92"))
    with sessions() as s:
        c = s.scalar(select(PaperPositionClose))
        assert c.reason == "TAKE_PROFIT" and c.net_pnl > 0
    reconcile(sessions, acc_id)


# --------------------------------------------------------------------------- integrity


def test_repeated_ticks_never_duplicate(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    for k in range(3):
        t = now + timedelta(seconds=3 * k)
        tick(sessions, acc_id, t, book(t, "80.00", "80.02"))
    assert count(sessions, PaperDecision) == 1
    assert count(sessions, PaperPosition) == 1
    assert count(sessions, PaperOrder) == 1


def test_bars_before_start_are_never_traded(env: Any) -> None:
    sessions, acc_id, script = env
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR)  # signal bar closes while the bot is STOPPED
    later = SIG_BAR + STEP + timedelta(minutes=1)
    with sessions.begin() as s:
        collector_ok(s, later)
    start(sessions, acc_id, later)
    tick(sessions, acc_id, later + timedelta(seconds=3), book(later, "80.00", "80.02"))
    assert count(sessions, PaperDecision) == 0 and count(sessions, PaperPosition) == 0


def test_signal_outside_entry_window_is_not_traded_in_hindsight(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR, hb_at=SIG_BAR + STEP + timedelta(minutes=5))
    late = SIG_BAR + STEP + timedelta(minutes=5)  # worker was busy / down for 5 minutes
    tick(sessions, acc_id, late, book(late, "80.00", "80.02"))
    with sessions() as s:
        d = s.scalar(select(PaperDecision))
        assert d.outcome == "BLOCKED" and d.block_reason == "ENTRY_WINDOW_MISSED"
    assert count(sessions, PaperPosition) == 0


def test_stale_book_degrades_and_blocks_entry_and_exit(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=60)
    stale = book(t - timedelta(minutes=5), "70.00", "70.02")  # would hit the stop if trusted
    tick(sessions, acc_id, t, stale)
    assert count(sessions, PaperPositionClose) == 0  # never exit on a stale price
    assert state(sessions, acc_id) == "DEGRADED"
    t2 = t + timedelta(seconds=5)
    tick(sessions, acc_id, t2, book(t2, "80.00", "80.02"))
    assert state(sessions, acc_id) == "RUNNING"


def test_wide_spread_and_missing_funding_block_entries(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR)
    now = SIG_BAR + STEP + timedelta(seconds=20)
    tick(sessions, acc_id, now, book(now, "79.80", "80.20"))  # 50 bps spread
    with sessions() as s:
        assert s.scalar(select(PaperDecision.block_reason)) == "SPREAD_TOO_WIDE"


# --------------------------------------------------------------------------- funding


def test_hourly_funding_recorded_and_assumed(env: Any) -> None:
    sessions, acc_id, _now = _open_long(env)  # opened 14:30:20
    h1 = utc(2026, 10, 13, 15)
    with sessions.begin() as s:
        add_bars(s, h1 - STEP, 1, "80")  # the 30m bar closing at 15:00 prices the notional
        funding(s, h1, "0.0001")  # positive: the long pays
        collector_ok(s, h1 + timedelta(minutes=1))
    t = h1 + timedelta(minutes=1)
    tick(sessions, acc_id, t, book(t, "80.00", "80.02"))
    with sessions() as s:
        pos = engine.open_position(s, acc_id)
        paid = engine.position_funding(s, pos)
        assert paid == (pos.size * D(80) * D("0.0001")).quantize(D("1e-8"))
    # 16:00 has no recorded rate: assumed only after the grace period
    h2 = utc(2026, 10, 13, 16)
    t2 = h2 + timedelta(minutes=10)
    with sessions.begin() as s:
        collector_ok(s, t2)
        funding(s, h2 - timedelta(minutes=30), "0.00001")  # keeps funding feed "fresh"
    tick(sessions, acc_id, t2, book(t2, "80.00", "80.02"))
    with sessions() as s:
        assert engine.position_funding(s, engine.open_position(s, acc_id)) == paid
    t3 = h2 + timedelta(minutes=31)
    with sessions.begin() as s:
        collector_ok(s, t3)
    tick(sessions, acc_id, t3, book(t3, "80.00", "80.02"))
    with sessions() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(PaperRiskEvent)
                .where(PaperRiskEvent.kind == "FUNDING_ASSUMED")
            )
            == 1
        )
    reconcile(sessions, acc_id)


# --------------------------------------------------------------------------- risk & control


def test_kill_switch_blocks_entries_and_close_and_stop_flattens(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=10)
    command(sessions, acc_id, "KILL", t, "kill-1")
    tick(sessions, acc_id, t, book(t, "80.00", "80.02"))
    assert state(sessions, acc_id) == "HALTED"
    assert count(sessions, PaperPositionClose) == 0  # kill = no new entries; stop/target stay
    t2 = t + timedelta(seconds=10)
    command(sessions, acc_id, "CLOSE_AND_STOP", t2, "close-1")
    tick(sessions, acc_id, t2, book(t2, "80.10", "80.12"))
    with sessions() as s:
        c = s.scalar(select(PaperPositionClose))
        assert c.reason == "MANUAL_CLOSE"
        assert engine.current_state(s, acc_id).state == "HALTED"  # halt survives until reviewed
    command(sessions, acc_id, "RESET_HALT", t2 + timedelta(seconds=5), "reset-1")
    tick(sessions, acc_id, t2 + timedelta(seconds=5), book(t2, "80.10", "80.12"))
    assert state(sessions, acc_id) == "STOPPED"
    reconcile(sessions, acc_id)


def test_stop_command_keeps_position_managed(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=10)
    command(sessions, acc_id, "STOP", t, "stop-1")
    tick(sessions, acc_id, t, book(t, "80.00", "80.02"))
    assert state(sessions, acc_id) == "STOPPED"
    t2 = t + timedelta(seconds=10)
    tick(sessions, acc_id, t2, book(t2, "78.40", "78.42"))  # stop still works while STOPPED
    assert count(sessions, PaperPositionClose) == 1


def test_three_losses_halt_the_bot(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    t = SIG_BAR
    for k in range(3):
        bar = SIG_BAR + 2 * k * STEP
        script[bar] = L
        close_bar(sessions, bar)
        now = bar + STEP + timedelta(seconds=20)
        tick(sessions, acc_id, now, book(now, "80.00", "80.02"))
        stop_t = now + timedelta(seconds=30)
        tick(sessions, acc_id, stop_t, book(stop_t, "78.40", "78.42"))
        close_bar(sessions, bar + STEP, hb_at=bar + 2 * STEP)
        t = bar + 2 * STEP
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "HALTED" and ev.reason == "CONSECUTIVE_LOSSES"
        assert engine.consecutive_losses(s, acc_id) == 3
    assert t > SIG_BAR
    reconcile(sessions, acc_id)


def test_daily_loss_limit_blocks_further_entries(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    with sessions.begin() as s:
        engine.risk_event(s, acc_id, "DAILY_LOSS_LIMIT", {"test": True}, T0)
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR)
    now = SIG_BAR + STEP + timedelta(seconds=20)
    tick(sessions, acc_id, now, book(now, "80.00", "80.02"))
    with sessions() as s:
        assert s.scalar(select(PaperDecision.block_reason)) == "DAILY_LOSS_LIMIT"


def test_drawdown_halt(env: Any) -> None:
    sessions, acc_id, now = _open_long(env)
    t = now + timedelta(seconds=10)
    # mark the open position so far down that equity < 95% of the high-water mark,
    # without touching the stop (stop at 78.537: a 1-tick-above book keeps it open)
    with sessions() as s:
        pos = engine.open_position(s, acc_id)
    crash_bid = pos.stop + D("0.01")
    # inflate the loss with a huge virtual size is not possible; instead lower the HWM test:
    # equity = 1000 - small loss; force the threshold by monkeypatching the config value
    with sessions.begin() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        cfg = PaperConfig()
        object.__setattr__(cfg.risk, "drawdown_halt_pct", D("0.1"))
        engine.risk_monitors(
            s, acc, cfg, engine.Market(now=t, book=book(t, crash_bid, crash_bid + D("0.02")))
        )
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "HALTED" and ev.reason == "DRAWDOWN_HALT"


def test_drawdown_halt_can_be_reset_and_rebases_the_limit(env: Any) -> None:
    """Review finding: the high-water mark never reset, so RESET_HALT after a drawdown
    halt was immediately undone on the same tick."""
    sessions, acc_id, now = _open_long(env)
    with sessions() as s:
        pos = engine.open_position(s, acc_id)
    low = pos.stop + D("0.01")
    cfg = PaperConfig()
    object.__setattr__(cfg.risk, "drawdown_halt_pct", D("0.1"))
    t = now + timedelta(seconds=10)
    with sessions.begin() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        engine.risk_monitors(s, acc, cfg, engine.Market(now=t, book=book(t, low, low + D("0.02"))))
    command(sessions, acc_id, "RESET_HALT", t + timedelta(seconds=5), "reset-dd-1")
    t2 = t + timedelta(seconds=6)
    with sessions.begin() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        m = engine.Market(now=t2, book=book(t2, low, low + D("0.02")))
        engine.apply_commands(s, acc, cfg, m)
        engine.risk_monitors(s, acc, cfg, m)  # same tick: must not halt again
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "STOPPED" and ev.reason == "HALT_RESET"
        acc = s.get(engine.PaperAccount, acc_id)
        assert engine.high_water(s, acc) > engine.high_water(s, acc, since_reset=True)


def test_a_late_committed_command_is_never_skipped(env: Any) -> None:
    """Review finding: commands were selected by id > max(handled id); a command that
    commits late with a lower id (two browser tabs) was skipped forever."""
    from autotrader.db.models import PaperBotEvent, PaperCommand

    sessions, acc_id, _ = env
    command(sessions, acc_id, "PAUSE", T0, "late-low-id")
    command(sessions, acc_id, "KILL", T0, "early-high-id")
    with sessions.begin() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        kill = s.scalar(select(PaperCommand).where(PaperCommand.command == "KILL"))
        engine._apply(s, acc, PaperConfig(), engine.Market(now=T0, book=None), kill)
    tick(sessions, acc_id, T0, book(T0, "80.00", "80.02"))
    with sessions() as s:
        assert engine.pending_commands(s, acc_id) == []
        pause = s.scalar(select(PaperCommand).where(PaperCommand.command == "PAUSE"))
        ev = s.scalar(select(PaperBotEvent).where(PaperBotEvent.command_id == pause.id))
        assert ev is not None and ev.reason == "REFUSED_PAUSE"
        risk = s.scalar(select(PaperRiskEvent).where(PaperRiskEvent.kind == "KILL_SWITCH"))
        assert risk.detail == {"by": "owner"}  # no username in the public event log


def test_day_start_ignores_the_midnight_slot_marked_after_midnight(env: Any) -> None:
    from autotrader.db.models import PaperEquitySnapshot

    sessions, acc_id, _ = env
    day = utc(2026, 10, 14)
    with sessions.begin() as s:
        s.add(PaperEquitySnapshot(account_id=acc_id, at=day - timedelta(minutes=5),
                                  cash=D(1000), unrealized=D(0), equity=D("1000")))  # fmt: skip
        # the 00:00 slot, marked at 00:04 after a loss
        s.add(PaperEquitySnapshot(account_id=acc_id, at=day, cash=D(1000),
                                  unrealized=D(-6), equity=D("994")))  # fmt: skip
    with sessions() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        assert engine.day_start_equity(s, acc, day + timedelta(hours=3)) == D("1000")


def test_restart_rechecks_and_skips_bars_from_downtime(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    script[SIG_BAR] = L
    close_bar(sessions, SIG_BAR)  # closes while the worker is "down"
    back = SIG_BAR + STEP + timedelta(minutes=2)
    with sessions.begin() as s:
        collector_ok(s, back)
        acc = s.get(engine.PaperAccount, acc_id)
        engine.recover_after_restart(
            s, acc, PaperConfig(), engine.Market(now=back, book=book(back, "80.00", "80.02"))
        )
    with sessions() as s:
        ev = engine.current_state(s, acc_id)
        assert ev.state == "RUNNING" and ev.reason == "RESTART_RECOVERY"
        assert engine.session_start(s, acc_id) == back
    tick(sessions, acc_id, back + timedelta(seconds=3), book(back, "80.00", "80.02"))
    assert count(sessions, PaperPosition) == 0  # the downtime bar is never traded


def test_max_entries_per_day(env: Any) -> None:
    sessions, acc_id, script = env
    start(sessions, acc_id)
    for k in range(5):
        bar = SIG_BAR + 2 * k * STEP
        script[bar] = L
        close_bar(sessions, bar)
        now = bar + STEP + timedelta(seconds=20)
        tick(sessions, acc_id, now, book(now, "80.00", "80.02"))
        tp = now + timedelta(seconds=30)
        tick(sessions, acc_id, tp, book(tp, "83.10", "83.12"))  # winners: no halt
        close_bar(sessions, bar + STEP, hb_at=bar + 2 * STEP)
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(PaperPosition)) == 4
        assert "MAX_ENTRIES_PER_DAY" in list(s.scalars(select(PaperDecision.block_reason)))
    reconcile(sessions, acc_id)


# --------------------------------------------------------------------------- book model


def test_parse_l2_rejects_crossed_and_malformed() -> None:
    bid = [{"px": "93.294", "sz": "259.986", "n": 2}]
    ask = [{"px": "93.307", "sz": "70.118", "n": 1}]
    raw = {"coin": COIN, "time": 1_793_802_803_902, "levels": [bid, ask]}
    b = bk.parse_l2(raw, COIN)
    assert b is not None and b.best_bid == D("93.294") and b.best_ask == D("93.307")
    crossed = {**raw, "levels": [[{"px": "93.4", "sz": "1"}], [{"px": "93.3", "sz": "1"}]]}
    assert bk.parse_l2(crossed, COIN) is None
    assert bk.parse_l2({**raw, "coin": "CL"}, COIN) is None
    assert bk.parse_l2({**raw, "levels": [[], []]}, COIN) is None
    assert (
        bk.parse_l2({**raw, "levels": [[{"px": "-1", "sz": "1"}], raw["levels"][1]]}, COIN) is None
    )


def test_walk_takes_levels_in_order_and_caps_depth() -> None:
    b = book(T0, "80.00", "80.02", size="1", levels=5)
    f = bk.walk(b, "BUY", D("2.5"), extra_slippage=D(0), sz_decimals=3)
    # 1 @ 80.02 + 1 @ 80.03 + 0.5 @ 80.04 = 200.07 / 2.5 = 80.028
    assert f.filled == D("2.5") and f.avg_price == D("80.028") and f.levels_used == 3
    capped = bk.walk(b, "BUY", D("10"), extra_slippage=D(0), sz_decimals=3, max_move=D("0.0002"))
    assert capped.filled == D("2")  # only levels within 2 bps of the touch
    sell = bk.walk(b, "SELL", D("1"), extra_slippage=D("0.0002"), sz_decimals=3)
    assert sell.avg_price == D("79.984")  # rounded down: never better than the book


def test_summary_numbers_come_from_the_ledger(env: Any) -> None:
    from autotrader.db.market_data import write_heartbeat
    from autotrader.paper import report

    sessions, acc_id, now = _open_long(env)
    with sessions.begin() as s:
        write_heartbeat(s, "paper-trader", state="RUNNING", ws_state="NOT_APPLICABLE",
                        started_at=now, last_heartbeat_at=now,
                        details={"book": {"time": now.isoformat(), "bid": "80.50", "ask": "80.52",
                                          "mid": "80.51", "spread_bps": "2.5"}})  # fmt: skip
    with sessions() as s:
        acc = s.get(engine.PaperAccount, acc_id)
        r = report.summary(s, acc, now + timedelta(seconds=1))
        pos = engine.open_position(s, acc_id)
        cash = engine.cash(s, acc_id)
    unreal = (D("80.50") - pos.entry_price) * pos.size  # marked at the BID for a long
    assert Decimal(r["pnl"]["unrealized"]) == unreal.quantize(D("1e-4"))
    assert Decimal(r["pnl"]["equity"]) == (cash + unreal).quantize(D("1e-4"))
    assert Decimal(r["pnl"]["total_pnl"]) == (cash + unreal - 1000).quantize(D("1e-4"))
    assert r["stats"]["open_positions"] == 1 and r["bot"]["state"] == "RUNNING"
    assert r["paper_only"] is True and "experimental" in r["account"]["experimental_warning"]
