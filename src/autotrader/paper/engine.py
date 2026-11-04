"""Autonomous PAPER execution engine (Phase 3). No code path here can reach an exchange:
it reads the database and an L2 snapshot from the public Info API, and writes
append-only simulation records.

One call to :func:`tick` (inside one DB transaction) does, in order:

1. apply owner commands (START / STOP / PAUSE / RESUME / KILL / CLOSE_AND_STOP /
   RESET_HALT) through the bot state machine (pre-start checks on START/RESUME);
2. manage the open position: hourly funding, stop / target against the live book;
3. risk monitors: drawdown halt, daily loss limit, consecutive-loss halt;
4. health: RUNNING <-> DEGRADED when data, book, funding, code or ledger is not OK;
5. decide every 30m bar that CLOSED since the bot started (never earlier), and
   open a position at the live book if every gate passes, within the entry window.

Entries are idempotent (unique decision per bar, unique client order id) and the
database itself refuses entries unless the bot is RUNNING, a second open position
and more than the daily entry limit (migration 0009).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autotrader.backtest.costs import floor_size, round_price
from autotrader.db.models import (
    CandleRow,
    FundingRate,
    PaperAccount,
    PaperBotEvent,
    PaperCommand,
    PaperDecision,
    PaperEquitySnapshot,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperPositionClose,
    PaperRiskEvent,
    ServiceHeartbeat,
)
from autotrader.market.candles import Interval
from autotrader.paper.book import L2Book, depth_at_or_better, walk
from autotrader.paper.config import PaperConfig, config_from_dict, paper_code_hash
from autotrader.strategy.base import Bar, Side
from autotrader.strategy.sessions import ReferenceSession
from autotrader.strategy.strategies import build_strategy

log = logging.getLogger("autotrader.paper")

BAR = timedelta(minutes=30)
HOUR = timedelta(hours=1)
ZERO = Decimal(0)
SESSION_START_REASONS = ("STARTED", "RESUMED", "RESTART_RECOVERY")
COLLECTOR_SERVICE = "market-data-collector"
COLLECTOR_MAX_AGE = timedelta(seconds=90)
SNAPSHOT_EVERY = timedelta(minutes=5)
#: how long to wait for the 1h bar that closes with an on-the-hour 30m bar
H1_WAIT = timedelta(minutes=3)


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _dec(v: object) -> Decimal:
    """SQL aggregate -> Decimal (NULL -> 0)."""
    return Decimal(str(v)) if v is not None else Decimal(0)


def _q(x: Decimal, places: int = 8) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places))


# =========================================================================== account


def create_account(
    session: Session,
    *,
    name: str,
    coin: str,
    now: datetime,
    config: PaperConfig | None = None,
) -> PaperAccount:
    """New virtual account: frozen config, initial deposit, state STOPPED."""
    from autotrader.paper.config import config_sha256
    from autotrader.strategy.base import params_hash

    cfg = config or PaperConfig()
    cfg.risk.validate()
    strat = build_strategy(cfg.strategy_id)
    anchor = session.scalar(
        select(func.min(CandleRow.open_time)).where(
            CandleRow.coin == coin, CandleRow.interval == "30m", CandleRow.is_closed.is_(True)
        )
    )
    if anchor is None:
        raise ValueError("no closed 30m candles stored yet; start the collector first")
    d = cfg.as_dict() | {"history_anchor": _utc(anchor).isoformat()}
    acc = PaperAccount(
        name=name,
        coin=coin,
        initial_equity=cfg.risk.initial_equity,
        strategy_id=cfg.strategy_id,
        strategy_version=strat.version,
        params_hash=params_hash(cfg.strategy_id, strat.version, strat.params),
        code_hash=paper_code_hash(),
        config=d,
        config_sha256=config_sha256(d),
        created_at=now,
    )
    session.add(acc)
    session.flush()
    session.add(
        PaperLedger(
            account_id=acc.id,
            kind="DEPOSIT",
            amount=cfg.risk.initial_equity,
            ref="initial",
            occurred_at=now,
        )
    )
    session.add(
        PaperBotEvent(
            account_id=acc.id, state="STOPPED", reason="CREATED", detail={}, occurred_at=now
        )
    )
    session.flush()
    return acc


# =========================================================================== reads


def current_state(session: Session, acc_id: int) -> PaperBotEvent:
    ev = session.scalar(
        select(PaperBotEvent)
        .where(PaperBotEvent.account_id == acc_id)
        .order_by(PaperBotEvent.id.desc())
        .limit(1)
    )
    assert ev is not None, "every account has a CREATED event"
    return ev


def session_start(session: Session, acc_id: int) -> datetime | None:
    t = session.scalar(
        select(PaperBotEvent.occurred_at)
        .where(
            PaperBotEvent.account_id == acc_id,
            PaperBotEvent.state == "RUNNING",
            PaperBotEvent.reason.in_(SESSION_START_REASONS),
        )
        .order_by(PaperBotEvent.id.desc())
        .limit(1)
    )
    return _utc(t) if t else None


def open_position(session: Session, acc_id: int) -> PaperPosition | None:
    closed = select(PaperPositionClose.position_id)
    return session.scalar(
        select(PaperPosition)
        .where(PaperPosition.account_id == acc_id, PaperPosition.id.not_in(closed))
        .order_by(PaperPosition.id.desc())
        .limit(1)
    )


def exact_sum(session: Session, column: Any, *conditions: Any) -> Decimal:
    """Exact Decimal sum computed in Python. (SQL SUM is exact on PostgreSQL NUMERIC but
    becomes binary floating point on SQLite; accounting must not depend on the backend.)"""
    return sum((Decimal(v) for v in session.scalars(select(column).where(*conditions))), ZERO)


def cash(session: Session, acc_id: int) -> Decimal:
    return exact_sum(session, PaperLedger.amount, PaperLedger.account_id == acc_id)


def entry_fee(session: Session, pos: PaperPosition) -> Decimal:
    o = session.get(PaperOrder, pos.entry_order_id)
    return o.fee if o else ZERO


def position_funding(session: Session, pos: PaperPosition) -> Decimal:
    """Funding PAID by the position so far (positive = cost)."""
    return -exact_sum(
        session,
        PaperLedger.amount,
        PaperLedger.account_id == pos.account_id,
        PaperLedger.kind == "FUNDING",
        PaperLedger.ref.like(f"funding:{pos.id}:%"),
    )


def unrealized(pos: PaperPosition, book: L2Book | None) -> Decimal | None:
    """Marked at the EXECUTABLE price (bid for a long, ask for a short): conservative."""
    if book is None:
        return None
    px = book.best_bid if pos.side == "LONG" else book.best_ask
    sign = 1 if pos.side == "LONG" else -1
    return sign * (px - pos.entry_price) * pos.size


def high_water(session: Session, acc: PaperAccount) -> Decimal:
    values = session.scalars(
        select(PaperEquitySnapshot.equity).where(PaperEquitySnapshot.account_id == acc.id)
    )
    return max([acc.initial_equity, *(Decimal(v) for v in values)])


def day_start_equity(session: Session, acc: PaperAccount, now: datetime) -> Decimal:
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    snap = session.scalar(
        select(PaperEquitySnapshot.equity)
        .where(PaperEquitySnapshot.account_id == acc.id, PaperEquitySnapshot.at <= midnight)
        .order_by(PaperEquitySnapshot.at.desc())
        .limit(1)
    )
    if snap is not None:
        return Decimal(snap)
    # no mark before midnight: cash at midnight (position open over midnight unmarked)
    v = exact_sum(
        session,
        PaperLedger.amount,
        PaperLedger.account_id == acc.id,
        PaperLedger.occurred_at <= midnight,
    )
    return v if v > 0 else acc.initial_equity


def consecutive_losses(session: Session, acc_id: int) -> int:
    reset = session.scalar(
        select(func.max(PaperBotEvent.occurred_at)).where(
            PaperBotEvent.account_id == acc_id, PaperBotEvent.reason == "HALT_RESET"
        )
    )
    q = (
        select(PaperPositionClose.net_pnl)
        .join(PaperPosition, PaperPosition.id == PaperPositionClose.position_id)
        .where(PaperPosition.account_id == acc_id)
        .order_by(PaperPositionClose.id.desc())
    )
    if reset is not None:
        q = q.where(PaperPositionClose.closed_at > reset)
    n = 0
    for net in session.scalars(q):
        if net < 0:
            n += 1
        else:
            break
    return n


def entries_today(session: Session, acc_id: int, now: datetime) -> int:
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return int(
        session.scalar(
            select(func.count()).where(
                PaperPosition.account_id == acc_id, PaperPosition.opened_at >= midnight
            )
        )
        or 0
    )


def daily_limit_hit(session: Session, acc_id: int, now: datetime) -> bool:
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return bool(
        session.scalar(
            select(func.count()).where(
                PaperRiskEvent.account_id == acc_id,
                PaperRiskEvent.kind == "DAILY_LOSS_LIMIT",
                PaperRiskEvent.occurred_at >= midnight,
            )
        )
    )


def ledger_reconciles(session: Session, acc: PaperAccount) -> tuple[bool, dict[str, str]]:
    """cash == initial + sum(net of closed positions) - (open position's entry fee + funding)."""
    c = cash(session, acc.id)
    closed_ids = select(PaperPosition.id).where(PaperPosition.account_id == acc.id)
    closed_net = exact_sum(
        session, PaperPositionClose.net_pnl, PaperPositionClose.position_id.in_(closed_ids)
    )
    pos = open_position(session, acc.id)
    open_costs = entry_fee(session, pos) + position_funding(session, pos) if pos else ZERO
    expected = acc.initial_equity + closed_net - open_costs
    ok = c == expected
    return ok, {"cash": str(c), "expected": str(expected)}


# =========================================================================== market


@dataclass
class Market:
    """What the engine knows about the market at ``now`` (from the worker)."""

    now: datetime
    book: L2Book | None
    book_error: str | None = None
    #: mark / oracle from the collector's activeAssetCtx (display + divergence)
    ctx: dict[str, Any] = field(default_factory=dict)


def collector_health(session: Session, coin: str, now: datetime, max_bar_age_s: int) -> list[str]:
    problems: list[str] = []
    hb = session.get(ServiceHeartbeat, COLLECTOR_SERVICE)
    if hb is None:
        problems.append("COLLECTOR_NOT_RUNNING")
    else:
        age = now - _utc(hb.last_heartbeat_at)
        if hb.state not in ("RUNNING", "DEGRADED") or age > COLLECTOR_MAX_AGE:
            problems.append("COLLECTOR_NOT_RUNNING")
        elif hb.ws_state != "CONNECTED":
            problems.append("COLLECTOR_WS_DISCONNECTED")
    last = session.scalar(
        select(func.max(CandleRow.close_time)).where(
            CandleRow.coin == coin, CandleRow.interval == "30m", CandleRow.is_closed.is_(True)
        )
    )
    if last is None or now - _utc(last) > timedelta(seconds=max_bar_age_s):
        problems.append("MARKET_DATA_STALE")
    return problems


def book_problem(book: L2Book | None, now: datetime, cfg: PaperConfig) -> str | None:
    if book is None:
        return "ORDER_BOOK_UNAVAILABLE"
    if book.age_s(now) > cfg.execution.max_book_age_s or book.age_s(now) < -5:
        return "ORDER_BOOK_STALE"
    return None


def funding_problem(
    session: Session, coin: str, now: datetime, cfg: PaperConfig
) -> tuple[str | None, Decimal | None]:
    row = session.scalar(
        select(FundingRate)
        .where(FundingRate.coin == coin)
        .order_by(FundingRate.hour.desc())
        .limit(1)
    )
    if row is None or now - _utc(row.time) > timedelta(seconds=cfg.execution.max_funding_age_s):
        return "FUNDING_DATA_MISSING", None
    if abs(row.rate) > cfg.execution.max_abs_funding_rate:
        return "ABNORMAL_FUNDING", row.rate
    return None, row.rate


def health_problems(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market
) -> list[str]:
    now = market.now
    problems = collector_health(session, acc.coin, now, cfg.execution.max_bar_age_s)
    bp = book_problem(market.book, now, cfg)
    if bp:
        problems.append(bp)
    fp, _ = funding_problem(session, acc.coin, now, cfg)
    if fp == "FUNDING_DATA_MISSING":
        problems.append(fp)
    if paper_code_hash() != acc.code_hash:
        problems.append("STRATEGY_CODE_CHANGED")
    ok, _ = ledger_reconciles(session, acc)
    if not ok:
        problems.append("ACCOUNT_INCONSISTENT")
    return problems


# =========================================================================== state machine


def set_state(
    session: Session,
    acc_id: int,
    state: str,
    reason: str,
    now: datetime,
    *,
    detail: dict[str, Any] | None = None,
    command_id: int | None = None,
) -> PaperBotEvent:
    ev = PaperBotEvent(
        account_id=acc_id,
        state=state,
        reason=reason,
        detail=detail or {},
        command_id=command_id,
        occurred_at=now,
    )
    session.add(ev)
    session.flush()
    log.info("paper bot state", extra={"account": acc_id, "state": state, "reason": reason})
    return ev


def risk_event(
    session: Session, acc_id: int, kind: str, detail: dict[str, Any], now: datetime
) -> None:
    session.add(PaperRiskEvent(account_id=acc_id, kind=kind, detail=detail, occurred_at=now))
    session.flush()


def preflight(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market
) -> list[dict[str, Any]]:
    """Start checks (shown in the UI). Every entry: {check, ok, detail}."""
    now = market.now
    coll = collector_health(session, acc.coin, now, cfg.execution.max_bar_age_s)
    bp = book_problem(market.book, now, cfg)
    fp, rate = funding_problem(session, acc.coin, now, cfg)
    ok_ledger, led = ledger_reconciles(session, acc)
    code_ok = paper_code_hash() == acc.code_hash
    return [
        {"check": "DATABASE", "ok": True, "detail": "transaction open"},
        {
            "check": "COLLECTOR",
            "ok": not [p for p in coll if p.startswith("COLLECTOR")],
            "detail": ",".join(coll) or "running, WS connected",
        },
        {
            "check": "MARKET_DATA_FRESH",
            "ok": "MARKET_DATA_STALE" not in coll,
            "detail": "closed 30m bar within limit" if "MARKET_DATA_STALE" not in coll else "stale",
        },
        {"check": "ORDER_BOOK", "ok": bp is None, "detail": bp or "fresh, not crossed"},
        {
            "check": "FUNDING_DATA",
            "ok": fp != "FUNDING_DATA_MISSING",
            "detail": fp or f"latest rate {rate}",
        },
        {
            "check": "STRATEGY_FROZEN",
            "ok": code_ok,
            "detail": f"{acc.strategy_id} v{acc.strategy_version}"
            if code_ok
            else "STRATEGY_CODE_CHANGED",
        },
        {
            "check": "RISK_LIMITS",
            "ok": True,
            "detail": f"risk {cfg.risk.risk_per_trade_pct}% / day {cfg.risk.daily_loss_limit_pct}%"
            f" / dd {cfg.risk.drawdown_halt_pct}% / {cfg.risk.max_leverage}x",
        },
        {"check": "LEDGER", "ok": ok_ledger, "detail": led},
        {"check": "SINGLE_ENGINE", "ok": True, "detail": "worker holds the account lock"},
        {"check": "PAPER_ONLY", "ok": True, "detail": "no exchange endpoint, no keys"},
    ]


def apply_commands(session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market) -> None:
    last = session.scalar(
        select(func.max(PaperBotEvent.command_id)).where(PaperBotEvent.account_id == acc.id)
    )
    cmds = session.scalars(
        select(PaperCommand)
        .where(PaperCommand.account_id == acc.id, PaperCommand.id > (last or 0))
        .order_by(PaperCommand.id)
    )
    for cmd in cmds:
        _apply(session, acc, cfg, market, cmd)


def _apply(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market, cmd: PaperCommand
) -> None:
    now = market.now
    st = current_state(session, acc.id).state
    c = cmd.command

    def refuse(why: str) -> None:
        set_state(session, acc.id, st, f"REFUSED_{c}", now, detail={"why": why}, command_id=cmd.id)

    if c in ("START", "RESUME"):
        allowed = ("STOPPED",) if c == "START" else ("PAUSED",)
        if st not in allowed:
            return refuse(f"state is {st}")
        checks = preflight(session, acc, cfg, market)
        set_state(
            session,
            acc.id,
            "STARTING",
            f"{c}_REQUESTED",
            now,
            detail={"checks": checks},
            command_id=cmd.id,
        )
        failed = [x["check"] for x in checks if not x["ok"]]
        if failed:
            set_state(
                session,
                acc.id,
                "STOPPED" if c == "START" else "PAUSED",
                "PREFLIGHT_FAILED",
                now,
                detail={"failed": failed, "checks": checks},
            )
        else:
            set_state(
                session,
                acc.id,
                "RUNNING",
                "STARTED" if c == "START" else "RESUMED",
                now,
                detail={"checks": checks},
            )
    elif c == "STOP":
        if st in ("STOPPED",):
            return refuse("already stopped")
        if st == "HALTED":
            return refuse("halted: use RESET_HALT after review")
        set_state(
            session,
            acc.id,
            "STOPPED",
            "STOPPED_BY_OWNER",
            now,
            command_id=cmd.id,
            detail={"policy": "open position stays managed by its stop and target"},
        )
    elif c == "PAUSE":
        if st not in ("RUNNING", "DEGRADED"):
            return refuse(f"state is {st}")
        set_state(session, acc.id, "PAUSED", "PAUSED_BY_OWNER", now, command_id=cmd.id)
    elif c == "KILL":
        set_state(
            session,
            acc.id,
            "HALTED",
            "KILL_SWITCH",
            now,
            command_id=cmd.id,
            detail={"policy": "no new entries; open position keeps stop/target"},
        )
        risk_event(session, acc.id, "KILL_SWITCH", {"by": cmd.requested_by}, now)
    elif c == "CLOSE_AND_STOP":
        pos = open_position(session, acc.id)
        new = "HALTED" if st == "HALTED" else "STOPPED"
        set_state(session, acc.id, new, "CLOSE_AND_STOP", now, command_id=cmd.id)
        if pos is not None and not close_position(
            session, acc, cfg, pos, market, purpose="EXIT_MANUAL", reason="MANUAL_CLOSE"
        ):
            risk_event(
                session,
                acc.id,
                "CLOSE_FAILED",
                {"why": book_problem(market.book, now, cfg) or "no fill"},
                now,
            )
    elif c == "RESET_HALT":
        if st != "HALTED":
            return refuse(f"state is {st}")
        set_state(
            session,
            acc.id,
            "STOPPED",
            "HALT_RESET",
            now,
            command_id=cmd.id,
            detail={"note": "owner reviewed the halt"},
        )
    return None


def recover_after_restart(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market
) -> None:
    """A worker (re)start never resumes blindly: a RUNNING bot re-runs the start checks.
    Bars that closed while the worker was down are never traded afterwards."""
    st = current_state(session, acc.id).state
    if st in ("RUNNING", "DEGRADED", "STARTING"):
        checks = preflight(session, acc, cfg, market)
        set_state(
            session, acc.id, "STARTING", "RESTART_CHECKS", market.now, detail={"checks": checks}
        )
        failed = [x["check"] for x in checks if not x["ok"]]
        set_state(
            session,
            acc.id,
            "DEGRADED" if failed else "RUNNING",
            "RESTART_DEGRADED" if failed else "RESTART_RECOVERY",
            market.now,
            detail={"failed": failed},
        )


# =========================================================================== positions


def _client_id(*parts: object) -> str:
    return ":".join(str(p) for p in parts)[:96]


def _funding_hours(pos: PaperPosition, until: datetime) -> list[datetime]:
    opened = _utc(pos.opened_at)
    h = opened.replace(minute=0, second=0, microsecond=0) + HOUR
    out = []
    while h <= until:
        out.append(h)
        h += HOUR
    return out


def accrue_funding(
    session: Session,
    acc: PaperAccount,
    cfg: PaperConfig,
    pos: PaperPosition,
    now: datetime,
    *,
    until: datetime | None = None,
    force: bool = False,
) -> None:
    """Charge each funding hour the position was open for, once (unique ledger ref).
    Recorded rate if available; after the grace period (or ``force`` at close) the
    disclosed adverse assumption."""
    end = until or now
    sign = 1 if pos.side == "LONG" else -1
    for h in _funding_hours(pos, end):
        ref = f"funding:{pos.id}:{h.isoformat()}"
        if session.scalar(
            select(PaperLedger.id).where(
                PaperLedger.account_id == acc.id,
                PaperLedger.kind == "FUNDING",
                PaperLedger.ref == ref,
            )
        ):
            continue
        rate = session.scalar(
            select(FundingRate.rate).where(FundingRate.coin == acc.coin, FundingRate.hour == h)
        )
        price = (
            session.scalar(
                select(CandleRow.close).where(
                    CandleRow.coin == acc.coin,
                    CandleRow.interval == "30m",
                    CandleRow.close_time == h,
                )
            )
            or pos.entry_price
        )
        notional = pos.size * Decimal(price)
        if rate is not None:
            cost = sign * notional * Decimal(rate)  # positive rate: longs pay
        elif force or now - h >= timedelta(seconds=cfg.execution.funding_grace_s):
            cost = notional * cfg.execution.missing_funding_rate
            risk_event(session, acc.id, "FUNDING_ASSUMED", {"hour": h.isoformat()}, now)
        else:
            continue  # wait for the recorded rate
        session.add(
            PaperLedger(
                account_id=acc.id, kind="FUNDING", amount=-_q(cost), ref=ref, occurred_at=now
            )
        )
    session.flush()


def close_position(
    session: Session,
    acc: PaperAccount,
    cfg: PaperConfig,
    pos: PaperPosition,
    market: Market,
    *,
    purpose: str,
    reason: str,
    limit_price: Decimal | None = None,
) -> bool:
    """Exit the whole position against the live book. ``limit_price`` = resting target
    (filled at the limit, only if the book shows enough size there). Returns False if
    no fill was possible (stale book / no depth)."""
    now = market.now
    book = market.book
    if book_problem(book, now, cfg) is not None:
        return False
    assert book is not None
    exit_side = "SELL" if pos.side == "LONG" else "BUY"
    ex = cfg.execution
    if limit_price is not None:
        if depth_at_or_better(book, exit_side, limit_price) < pos.size:
            return False
        price: Decimal = limit_price
        filled = pos.size
        ref_px = limit_price
    else:
        f = walk(
            book,
            exit_side,
            pos.size,
            extra_slippage=ex.extra_slippage_rate,
            sz_decimals=ex.sz_decimals,
        )
        if f.avg_price is None:
            return False
        price, filled, ref_px = f.avg_price, f.filled, f.touch
        if filled < pos.size:
            # beyond the visible 20 levels: remainder at the worst level, worse by the buffer
            worst = (book.bids if exit_side == "SELL" else book.asks)[-1][0]
            rest = pos.size - filled
            worst_px = (
                worst * (1 - ex.extra_slippage_rate)
                if exit_side == "SELL"
                else worst * (1 + ex.extra_slippage_rate)
            )
            price = round_price(
                (price * filled + worst_px * rest) / pos.size,
                ex.sz_decimals,
                "down" if exit_side == "SELL" else "up",
            )
            filled = pos.size
    fee = _q(ex.taker_fee_rate * price * filled)
    order = PaperOrder(
        account_id=acc.id,
        client_order_id=_client_id("exit", pos.id),
        purpose=purpose,
        side=exit_side,
        requested_size=pos.size,
        filled_size=filled,
        avg_price=price,
        reference_price=ref_px,
        fee=fee,
        status="FILLED",
        book=book.snapshot(),
        created_at=now,
    )
    session.add(order)
    session.flush()
    accrue_funding(session, acc, cfg, pos, now, until=now, force=True)
    sign = 1 if pos.side == "LONG" else -1
    gross = _q(sign * (price - pos.entry_price) * pos.size)
    fees = entry_fee(session, pos) + fee
    funding = position_funding(session, pos)
    net = gross - fees - funding
    session.add(
        PaperPositionClose(
            position_id=pos.id,
            exit_order_id=order.id,
            reason=reason,
            exit_price=price,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            net_pnl=net,
            closed_at=now,
        )
    )
    session.add(
        PaperLedger(
            account_id=acc.id,
            kind="REALIZED_PNL",
            amount=gross,
            ref=f"position:{pos.id}",
            occurred_at=now,
        )
    )
    session.add(
        PaperLedger(
            account_id=acc.id, kind="FEE", amount=-fee, ref=f"order:{order.id}", occurred_at=now
        )
    )
    session.flush()
    log.info("paper position closed", extra={"position": pos.id, "reason": reason, "net": str(net)})
    if (
        net < 0
        and consecutive_losses(session, acc.id) >= cfg.risk.halt_after_consecutive_losses
        and current_state(session, acc.id).state != "HALTED"
    ):
        set_state(
            session,
            acc.id,
            "HALTED",
            "CONSECUTIVE_LOSSES",
            now,
            detail={"losses": cfg.risk.halt_after_consecutive_losses},
        )
        risk_event(
            session,
            acc.id,
            "CONSECUTIVE_LOSSES",
            {"losses": cfg.risk.halt_after_consecutive_losses},
            now,
        )
    return True


def manage_position(session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market) -> None:
    pos = open_position(session, acc.id)
    if pos is None:
        return
    accrue_funding(session, acc, cfg, pos, market.now)
    book = market.book
    if book_problem(book, market.now, cfg) is not None:
        return  # can't see the market: no exit decision on stale prices
    assert book is not None
    if pos.side == "LONG":
        if book.best_bid <= pos.stop:
            close_position(session, acc, cfg, pos, market, purpose="EXIT_STOP", reason="STOP")
        elif book.best_bid >= pos.target:
            close_position(
                session,
                acc,
                cfg,
                pos,
                market,
                purpose="EXIT_TARGET",
                reason="TAKE_PROFIT",
                limit_price=pos.target,
            )
    else:
        if book.best_ask >= pos.stop:
            close_position(session, acc, cfg, pos, market, purpose="EXIT_STOP", reason="STOP")
        elif book.best_ask <= pos.target:
            close_position(
                session,
                acc,
                cfg,
                pos,
                market,
                purpose="EXIT_TARGET",
                reason="TAKE_PROFIT",
                limit_price=pos.target,
            )


# =========================================================================== risk / health


def equity_now(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market
) -> tuple[Decimal, Decimal | None]:
    """(equity, unrealized). Unrealized is None if a position is open but the book is
    not fresh (the position can't be marked honestly)."""
    c = cash(session, acc.id)
    pos = open_position(session, acc.id)
    if pos is None:
        return c, ZERO
    fresh = book_problem(market.book, market.now, cfg) is None
    u = unrealized(pos, market.book if fresh else None)
    return (c + u, u) if u is not None else (c, None)


def risk_monitors(session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market) -> None:
    now = market.now
    eq, unreal = equity_now(session, acc, cfg, market)
    if unreal is None:
        return  # position open but no fresh book: can't mark it, monitors wait
    st = current_state(session, acc.id).state
    hwm = max(high_water(session, acc), eq)
    if st != "HALTED" and eq <= hwm * (1 - cfg.risk.drawdown_halt_pct / 100):
        set_state(
            session,
            acc.id,
            "HALTED",
            "DRAWDOWN_HALT",
            now,
            detail={"equity": str(_q(eq, 4)), "high_water": str(_q(hwm, 4))},
        )
        risk_event(
            session,
            acc.id,
            "DRAWDOWN_HALT",
            {"equity": str(_q(eq, 4)), "high_water": str(_q(hwm, 4))},
            now,
        )
    start = day_start_equity(session, acc, now)
    if not daily_limit_hit(session, acc.id, now) and eq <= start * (
        1 - cfg.risk.daily_loss_limit_pct / 100
    ):
        risk_event(
            session,
            acc.id,
            "DAILY_LOSS_LIMIT",
            {"equity": str(_q(eq, 4)), "day_start": str(_q(start, 4))},
            now,
        )
    # equity curve
    slot = datetime.fromtimestamp(
        (int(now.timestamp()) // int(SNAPSHOT_EVERY.total_seconds()))
        * int(SNAPSHOT_EVERY.total_seconds()),
        tz=UTC,
    )
    exists = session.scalar(
        select(PaperEquitySnapshot.id).where(
            PaperEquitySnapshot.account_id == acc.id, PaperEquitySnapshot.at == slot
        )
    )
    if not exists:
        session.add(
            PaperEquitySnapshot(
                account_id=acc.id,
                at=slot,
                cash=cash(session, acc.id),
                unrealized=_q(unreal),
                equity=_q(eq),
            )
        )
        session.flush()


def update_health(session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market) -> None:
    ev = current_state(session, acc.id)
    if ev.state not in ("RUNNING", "DEGRADED"):
        return
    problems = health_problems(session, acc, cfg, market)
    if ev.state == "RUNNING" and problems:
        set_state(
            session, acc.id, "DEGRADED", problems[0], market.now, detail={"problems": problems}
        )
    elif ev.state == "DEGRADED" and not problems:
        set_state(session, acc.id, "RUNNING", "RECOVERED", market.now)
    elif ev.state == "DEGRADED" and problems != ev.detail.get("problems"):
        set_state(
            session, acc.id, "DEGRADED", problems[0], market.now, detail={"problems": problems}
        )


# =========================================================================== decisions


def _bars(
    session: Session, coin: str, interval: Interval, anchor: datetime, now: datetime
) -> list[Bar]:
    rows = session.scalars(
        select(CandleRow)
        .where(
            CandleRow.coin == coin,
            CandleRow.interval == interval.value,
            CandleRow.is_closed.is_(True),
            CandleRow.close_time <= now,
            CandleRow.open_time >= anchor,
        )
        .order_by(CandleRow.open_time)
    )
    return [
        Bar(
            _utc(r.open_time),
            _utc(r.close_time),
            r.open,
            r.high,
            r.low,
            r.close,
            r.volume,
            r.trades,
        )
        for r in rows
    ]


def decide_new_bars(session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market) -> None:
    ev = current_state(session, acc.id)
    if ev.state not in ("RUNNING", "DEGRADED"):
        return
    since = session_start(session, acc.id)
    if since is None:
        return
    now = market.now
    anchor = datetime.fromisoformat(acc.config["history_anchor"])
    m30 = _bars(session, acc.coin, Interval.M30, anchor, now)
    h1 = _bars(session, acc.coin, Interval.H1, anchor, now)
    last = session.scalar(
        select(func.max(PaperDecision.bar_open_time)).where(PaperDecision.account_id == acc.id)
    )
    last = _utc(last) if last else None
    todo = [
        i
        for i, b in enumerate(m30)
        if b.close_time > since and (last is None or b.open_time > last)
    ]
    if not todo:
        return
    h1_last = h1[-1].close_time if h1 else None
    strat = build_strategy(acc.strategy_id)
    prepared = None
    for i in todo:
        bar = m30[i]
        need = bar.close_time.replace(minute=0, second=0, microsecond=0)
        if (h1_last is None or h1_last < need) and now - bar.close_time < H1_WAIT:
            break  # the 1h bar closing with this bar is not stored yet: decide next tick
        if prepared is None:
            prepared = strat.prepare(m30, h1)
        d = prepared.decide(i)
        _record_and_maybe_enter(session, acc, cfg, market, bar, d)


def _record_and_maybe_enter(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market, bar: Bar, d: Any
) -> None:
    now = market.now
    book = market.book
    snap: dict[str, Any] = {
        "bar": {
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
        },
        "book": book.snapshot(3) if book else None,
        "mark_px": market.ctx.get("markPx"),
        "oracle_px": market.ctx.get("oraclePx"),
    }
    dec = PaperDecision(
        account_id=acc.id,
        bar_open_time=bar.open_time,
        bar_close_time=bar.close_time,
        evaluated_at=now,
        side=d.side.value if d.side else None,
        reason=d.reason,
        outcome="NO_SIGNAL",
        atr=d.atr.quantize(Decimal("1e-18")) if d.atr is not None else None,
        rationale=dict(d.rationale),
        market=snap,
    )
    if d.side is None:
        session.add(dec)
        session.flush()
        return
    block = _entry_block(session, acc, cfg, market, bar, d)
    if block == "IN_POSITION":
        dec.outcome = "IN_POSITION"
        session.add(dec)
        session.flush()
        return
    if block is not None:
        dec.outcome, dec.block_reason = "BLOCKED", block
        session.add(dec)
        session.flush()
        risk_event(
            session,
            acc.id,
            "ENTRY_BLOCKED",
            {"bar": bar.open_time.isoformat(), "reason": block},
            now,
        )
        return
    plan = _plan_entry(session, acc, cfg, market, d.side, d.atr)
    if isinstance(plan, str):  # sizing / depth refused the trade
        dec.outcome, dec.block_reason = "BLOCKED", plan
        session.add(dec)
        session.flush()
        risk_event(
            session,
            acc.id,
            "ENTRY_BLOCKED",
            {"bar": bar.open_time.isoformat(), "reason": plan},
            now,
        )
        return
    dec.outcome = "ORDER"
    session.add(dec)
    session.flush()
    _open(session, acc, cfg, market, dec, plan)


def _entry_block(
    session: Session, acc: PaperAccount, cfg: PaperConfig, market: Market, bar: Bar, d: Any
) -> str | None:
    now = market.now
    st = current_state(session, acc.id)
    if open_position(session, acc.id) is not None:
        return "IN_POSITION"
    if st.state != "RUNNING":
        return f"BOT_{st.state}:{st.reason}"
    if now - bar.close_time > timedelta(seconds=cfg.execution.entry_window_s):
        return "ENTRY_WINDOW_MISSED"  # never trade a signal in hindsight
    if cfg.execution.session_filter:
        why = ReferenceSession().entry_block_reason(now)
        if why:
            return why
    if daily_limit_hit(session, acc.id, now):
        return "DAILY_LOSS_LIMIT"
    if entries_today(session, acc.id, now) >= cfg.risk.max_entries_per_day:
        return "MAX_ENTRIES_PER_DAY"
    if d.atr is None or d.atr <= 0:
        return "INVALID_ATR"
    if bar.high - bar.low > cfg.execution.max_signal_range_atr * d.atr:
        return "ABNORMAL_VOLATILITY"
    fp, _ = funding_problem(session, acc.coin, now, cfg)
    if fp:
        return fp
    bp = book_problem(market.book, now, cfg)
    if bp:
        return bp
    assert market.book is not None
    if market.book.spread_bps > cfg.execution.max_spread_bps:
        return "SPREAD_TOO_WIDE"
    ok, _ = ledger_reconciles(session, acc)
    if not ok:
        return "ACCOUNT_INCONSISTENT"
    if paper_code_hash() != acc.code_hash:
        return "STRATEGY_CODE_CHANGED"
    return None


@dataclass(frozen=True)
class EntryPlan:
    side: Side
    requested: Decimal
    filled: Decimal
    fill: Decimal
    touch: Decimal
    stop: Decimal
    target: Decimal
    atr: Decimal
    fee: Decimal
    budget: Decimal
    planned: Decimal


def _plan_entry(
    session: Session,
    acc: PaperAccount,
    cfg: PaperConfig,
    market: Market,
    side: Side,
    atr: Decimal,
) -> EntryPlan | str:
    """Size and price an entry against the live book. Returns a plan or a block reason.
    The risk budget is re-checked with the ACTUAL simulated fill (after walking the book,
    slippage and fees); the size shrinks until it fits, or the trade is refused."""
    book = market.book
    assert book is not None
    ex, rk = cfg.execution, cfg.risk
    strat = build_strategy(acc.strategy_id)
    sign = side.sign
    buy_sell = "BUY" if side is Side.LONG else "SELL"
    equity = cash(session, acc.id)  # flat: equity == cash
    budget = equity * rk.risk_per_trade_pct / 100
    stop_dist = strat.params.stop_atr * atr
    touch = book.best_ask if side is Side.LONG else book.best_bid
    est = touch * (1 + sign * ex.extra_slippage_rate)

    def per_unit(fill: Decimal, stop: Decimal) -> Decimal:
        stop_fill = stop * (1 - sign * ex.stop_slippage_estimate)
        return abs(fill - stop_fill) + ex.taker_fee_rate * (fill + stop_fill)

    if est - sign * stop_dist <= 0:
        return "INVALID_STOP"
    size = min(
        floor_size(budget / per_unit(est, est - sign * stop_dist), ex.sz_decimals),
        floor_size(rk.max_leverage * equity / est, ex.sz_decimals),
    )
    for _ in range(5):
        if size <= 0:
            return "SIZE_BELOW_MINIMUM"
        f = walk(
            book,
            buy_sell,
            size,
            extra_slippage=ex.extra_slippage_rate,
            sz_decimals=ex.sz_decimals,
            max_move=ex.max_entry_depth_move,
        )
        if f.avg_price is None:
            return "INSUFFICIENT_DEPTH"
        fill = f.avg_price
        raw_stop = fill - sign * stop_dist
        if raw_stop <= 0:
            return "INVALID_STOP"
        stop = round_price(raw_stop, ex.sz_decimals)
        if (sign > 0 and stop >= fill) or (sign < 0 and stop <= fill):
            return "INVALID_STOP"
        planned = f.filled * per_unit(fill, stop)
        if planned <= budget and f.filled * fill <= rk.max_leverage * equity:
            break
        size = min(
            floor_size(budget / per_unit(fill, stop), ex.sz_decimals),
            floor_size(rk.max_leverage * equity / fill, ex.sz_decimals),
            size - Decimal(1).scaleb(-ex.sz_decimals),
        )
    else:
        return "RISK_BUDGET"
    if f.filled * fill < rk.min_notional:
        return "SIZE_BELOW_MINIMUM"
    r = abs(fill - stop)
    target = round_price(fill + sign * strat.params.tp_r * r, ex.sz_decimals)
    return EntryPlan(
        side=side,
        requested=size,
        filled=f.filled,
        fill=fill,
        touch=f.touch,
        stop=stop,
        target=target,
        atr=atr.quantize(Decimal("1e-18")),
        fee=_q(ex.taker_fee_rate * fill * f.filled),
        budget=_q(budget),
        planned=_q(planned),
    )


def _open(
    session: Session,
    acc: PaperAccount,
    cfg: PaperConfig,
    market: Market,
    dec: PaperDecision,
    plan: EntryPlan,
) -> None:
    now = market.now
    assert market.book is not None
    order = PaperOrder(
        account_id=acc.id,
        client_order_id=_client_id("entry", acc.id, dec.bar_open_time.isoformat()),
        purpose="ENTRY",
        side="BUY" if plan.side is Side.LONG else "SELL",
        requested_size=plan.requested,
        filled_size=plan.filled,
        avg_price=plan.fill,
        reference_price=plan.touch,
        fee=plan.fee,
        status="FILLED" if plan.filled == plan.requested else "PARTIALLY_FILLED",
        book=market.book.snapshot(),
        created_at=now,
    )
    session.add(order)
    session.flush()
    session.add(
        PaperPosition(
            account_id=acc.id,
            decision_id=dec.id,
            entry_order_id=order.id,
            side=plan.side.value,
            size=plan.filled,
            entry_price=plan.fill,
            stop=plan.stop,
            target=plan.target,
            atr=plan.atr,
            risk_budget=plan.budget,
            planned_loss=plan.planned,
            opened_at=now,
        )
    )
    session.add(
        PaperLedger(
            account_id=acc.id,
            kind="FEE",
            amount=-plan.fee,
            ref=f"order:{order.id}",
            occurred_at=now,
        )
    )
    session.flush()
    log.info(
        "paper position opened",
        extra={"side": plan.side.value, "size": str(plan.filled), "price": str(plan.fill)},
    )


# =========================================================================== tick


def tick(session: Session, acc: PaperAccount, market: Market) -> None:
    cfg = config_from_dict({k: v for k, v in acc.config.items() if k != "history_anchor"})
    apply_commands(session, acc, cfg, market)
    manage_position(session, acc, cfg, market)
    risk_monitors(session, acc, cfg, market)
    update_health(session, acc, cfg, market)
    decide_new_bars(session, acc, cfg, market)


def accounts(session: Session, coin: str) -> Sequence[PaperAccount]:
    return list(
        session.scalars(
            select(PaperAccount).where(PaperAccount.coin == coin).order_by(PaperAccount.id)
        )
    )
