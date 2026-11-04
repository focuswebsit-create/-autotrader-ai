"""Read model for the terminal: every number is computed from the PAPER ledger,
positions and the live quote the worker published. Nothing is estimated from
backtests, and nothing is invented when data is missing (then it says so)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autotrader.db.models import (
    PaperAccount,
    PaperBotEvent,
    PaperDecision,
    PaperEquitySnapshot,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperPositionClose,
    PaperRiskEvent,
    ServiceHeartbeat,
)
from autotrader.paper import engine
from autotrader.paper.config import config_from_dict
from autotrader.paper.worker import SERVICE_NAME

QUOTE_MAX_AGE = timedelta(seconds=20)


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _iso(t: datetime | None) -> str | None:
    u = _utc(t)
    return u.isoformat() if u else None


def _s(x: Decimal | None, places: int = 4) -> str | None:
    return None if x is None else str(x.quantize(Decimal(1).scaleb(-places)))


def _sum(session: Session, acc_id: int, kind: str, since: datetime | None = None) -> Decimal:
    conds = [PaperLedger.account_id == acc_id, PaperLedger.kind == kind]
    if since is not None:
        conds.append(PaperLedger.occurred_at >= since)
    return engine.exact_sum(session, PaperLedger.amount, *conds)


def get_account(session: Session, coin: str, name: str) -> PaperAccount | None:
    return session.scalar(
        select(PaperAccount).where(PaperAccount.coin == coin, PaperAccount.name == name)
    )


def worker_quote(session: Session, now: datetime) -> dict[str, Any]:
    hb = session.get(ServiceHeartbeat, SERVICE_NAME)
    if hb is None:
        return {"worker": "NOT_RUNNING", "fresh": False}
    det = hb.details or {}
    age = (now - (_utc(hb.last_heartbeat_at) or now)).total_seconds()
    book = det.get("book") or None
    fresh = False
    if book and hb.state == "RUNNING" and age < QUOTE_MAX_AGE.total_seconds():
        bt = datetime.fromisoformat(book["time"])
        fresh = (now - bt) < QUOTE_MAX_AGE
    return {
        "worker": hb.state if age < 60 else "DOWN",
        "heartbeat_age_s": round(age, 1),
        "book": book,
        "book_error": det.get("book_error"),
        "fresh": fresh,
        "market_ctx": det.get("market_ctx") or {},
        "last_funding_sync": det.get("last_funding_sync"),
    }


def summary(session: Session, acc: PaperAccount, now: datetime) -> dict[str, Any]:
    cfg = config_from_dict({k: v for k, v in acc.config.items() if k != "history_anchor"})
    ev = engine.current_state(session, acc.id)
    quote = worker_quote(session, now)
    cash = engine.cash(session, acc.id)
    pos = engine.open_position(session, acc.id)
    unreal: Decimal | None = Decimal(0)
    pos_json: dict[str, Any] | None = None
    if pos is not None:
        unreal = None
        mark_px = None
        if quote["fresh"]:
            px = Decimal(quote["book"]["bid"] if pos.side == "LONG" else quote["book"]["ask"])
            sign = 1 if pos.side == "LONG" else -1
            unreal = sign * (px - pos.entry_price) * pos.size
            mark_px = px
        dec = session.get(PaperDecision, pos.decision_id)
        pos_json = {
            "id": pos.id,
            "side": pos.side,
            "size": str(pos.size),
            "entry_price": str(pos.entry_price),
            "stop": str(pos.stop),
            "target": str(pos.target),
            "opened_at": _iso(pos.opened_at),
            "exit_price_now": str(mark_px) if mark_px is not None else None,
            "unrealized": _s(unreal),
            "unrealized_note": "marked at the executable price (bid for LONG, ask for SHORT)"
            if unreal is not None
            else "PRICE_STALE: no fresh order book, not marked",
            "fees_paid": _s(engine.entry_fee(session, pos)),
            "funding_paid": _s(engine.position_funding(session, pos)),
            "risk_budget": _s(pos.risk_budget),
            "planned_loss": _s(pos.planned_loss),
            "open_reason": (dec.rationale or {}).get("rule") if dec else None,
            "signal_bar": _iso(dec.bar_open_time) if dec else None,
        }
    equity = cash + unreal if unreal is not None else None
    closes = list(
        session.execute(
            select(PaperPositionClose, PaperPosition)
            .join(PaperPosition, PaperPosition.id == PaperPositionClose.position_id)
            .where(PaperPosition.account_id == acc.id)
        )
    )
    nets = [c.net_pnl for c, _ in closes]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    gl = -sum(losses, Decimal(0))
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    day_start = engine.day_start_equity(session, acc, now)
    today_realized = sum(
        (c.net_pnl for c, _ in closes if (_utc(c.closed_at) or now) >= midnight), Decimal(0)
    )
    snaps = list(
        session.scalars(
            select(PaperEquitySnapshot.equity)
            .where(PaperEquitySnapshot.account_id == acc.id)
            .order_by(PaperEquitySnapshot.at)
        )
    )
    hwm, mdd = acc.initial_equity, Decimal(0)
    for e in [*snaps, *([equity] if equity is not None else [])]:
        hwm = max(hwm, e)
        if hwm > 0:
            mdd = max(mdd, (hwm - e) / hwm * 100)
    initial = acc.initial_equity
    last_dec = session.scalar(
        select(PaperDecision)
        .where(PaperDecision.account_id == acc.id)
        .order_by(PaperDecision.id.desc())
        .limit(1)
    )
    return {
        "paper_only": True,
        "now": now.isoformat(),
        "account": {
            "name": acc.name,
            "strategy_id": acc.strategy_id,
            "strategy_version": acc.strategy_version,
            "params_hash": acc.params_hash,
            "initial_equity": _s(initial, 2),
            "created_at": _iso(acc.created_at),
            "risk": acc.config.get("risk"),
            "experimental_warning": "Strategies are experimental: no strategy has shown a "
            "statistically proven edge. Virtual money only.",
        },
        "bot": {
            "state": ev.state,
            "reason": ev.reason,
            "since": _iso(ev.occurred_at),
            "detail": ev.detail,
            "session_start": _iso(engine.session_start(session, acc.id)),
        },
        "market": quote,
        "pnl": {
            "cash": _s(cash),
            "equity": _s(equity),
            "unrealized": _s(unreal),
            "realized_net": _s(sum(nets, Decimal(0))),
            "gross_realized": _s(_sum(session, acc.id, "REALIZED_PNL")),
            "fees": _s(-_sum(session, acc.id, "FEE")),
            "funding": _s(-_sum(session, acc.id, "FUNDING")),
            "total_pnl": _s(equity - initial) if equity is not None else None,
            "total_pct": _s((equity - initial) / initial * 100, 3) if equity is not None else None,
            "day_start_equity": _s(day_start),
            "today_pnl": _s(equity - day_start) if equity is not None else None,
            "today_realized": _s(today_realized),
            "today_unrealized": _s(unreal),
            "day_basis": "UTC calendar day",
        },
        "stats": {
            "closed_trades": len(nets),
            "open_positions": 1 if pos is not None else 0,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(nets), 4) if nets else None,
            "profit_factor": (
                round(float(sum(wins, Decimal(0)) / gl), 3)
                if gl > 0
                else (None if not wins else "inf")
            ),
            "max_drawdown_pct": _s(mdd, 3),
        },
        "position": pos_json,
        "risk": {
            "entries_today": engine.entries_today(session, acc.id, now),
            "max_entries_per_day": cfg.risk.max_entries_per_day,
            "daily_loss_limit_hit": engine.daily_limit_hit(session, acc.id, now),
            "consecutive_losses": engine.consecutive_losses(session, acc.id),
            "halt_after_consecutive_losses": cfg.risk.halt_after_consecutive_losses,
            "high_water": _s(hwm),
            "drawdown_halt_pct": str(cfg.risk.drawdown_halt_pct),
            "daily_loss_limit_pct": str(cfg.risk.daily_loss_limit_pct),
            "risk_per_trade_pct": str(cfg.risk.risk_per_trade_pct),
            "max_leverage": str(cfg.risk.max_leverage),
        },
        "last_decision": decision_json(last_dec) if last_dec else None,
        "readiness": readiness(session, acc, now),
    }


def readiness(session: Session, acc: PaperAccount, now: datetime) -> dict[str, Any]:
    """Pre-flight checks as the running worker last evaluated them (heartbeat).

    The terminal enables "start" only when these are all OK and fresh: the worker
    re-runs exactly the same checks when it executes START, so this is a preview,
    never a bypass.
    """
    hb = session.get(ServiceHeartbeat, SERVICE_NAME)
    if hb is None:
        return {"ok": False, "fresh": False, "reason": "PAPER_WORKER_NOT_RUNNING", "checks": []}
    age = (now - (_utc(hb.last_heartbeat_at) or now)).total_seconds()
    r = ((hb.details or {}).get("readiness") or {}).get(str(acc.id))
    if hb.state != "RUNNING" or age >= 60:
        return {"ok": False, "fresh": False, "reason": "PAPER_WORKER_DOWN", "checks": []}
    if not isinstance(r, dict):
        return {"ok": False, "fresh": False, "reason": "NO_READINESS_YET", "checks": []}
    return {
        "ok": bool(r.get("ok")),
        "fresh": True,
        "reason": None if r.get("ok") else "CHECK_FAILED",
        "at": r.get("at"),
        "checks": r.get("checks") or [],
    }


def decision_json(d: PaperDecision) -> dict[str, Any]:
    return {
        "bar_open_time": _iso(d.bar_open_time),
        "bar_close_time": _iso(d.bar_close_time),
        "evaluated_at": _iso(d.evaluated_at),
        "side": d.side,
        "reason": d.reason,
        "outcome": d.outcome,
        "block_reason": d.block_reason,
        "atr": str(d.atr) if d.atr is not None else None,
        "rationale": d.rationale,
        "market": d.market,
    }


def trades(session: Session, acc: PaperAccount, now: datetime, period: str) -> list[dict[str, Any]]:
    since = {
        "day": datetime(now.year, now.month, now.day, tzinfo=UTC),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }.get(period)
    q = select(PaperPosition).where(PaperPosition.account_id == acc.id)
    if since is not None:
        q = q.where(PaperPosition.opened_at >= since)
    out = []
    for p in session.scalars(q.order_by(PaperPosition.id.desc()).limit(500)):
        c = session.scalar(select(PaperPositionClose).where(PaperPositionClose.position_id == p.id))
        entry = session.get(PaperOrder, p.entry_order_id)
        dec = session.get(PaperDecision, p.decision_id)
        out.append(
            {
                "id": p.id,
                "status": "CLOSED" if c else "OPEN",
                "side": p.side,
                "strategy": acc.strategy_id,
                "opened_at": _iso(p.opened_at),
                "closed_at": _iso(c.closed_at) if c else None,
                "entry_price": str(p.entry_price),
                "exit_price": str(c.exit_price) if c else None,
                "size": str(p.size),
                "stop": str(p.stop),
                "target": str(p.target),
                "entry_fill": entry.status if entry else None,
                "entry_touch": str(entry.reference_price)
                if entry and entry.reference_price
                else None,
                "fees": _s(c.fees if c else (entry.fee if entry else None)),
                "funding": _s(c.funding) if c else _s(engine.position_funding(session, p)),
                "gross_pnl": _s(c.gross_pnl) if c else None,
                "net_pnl": _s(c.net_pnl) if c else None,
                "open_reason": (dec.rationale or {}).get("rule") if dec else None,
                "close_reason": c.reason if c else None,
                "signal_bar": _iso(dec.bar_open_time) if dec else None,
            }
        )
    return out


def decisions(session: Session, acc: PaperAccount, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(PaperDecision)
        .where(PaperDecision.account_id == acc.id)
        .order_by(PaperDecision.id.desc())
        .limit(limit)
    )
    return [decision_json(d) for d in rows]


def events(session: Session, acc: PaperAccount, limit: int) -> list[dict[str, Any]]:
    bot = [
        {
            "time": _iso(e.occurred_at),
            "type": "BOT",
            "kind": e.state,
            "reason": e.reason,
            "detail": e.detail,
        }
        for e in session.scalars(
            select(PaperBotEvent)
            .where(PaperBotEvent.account_id == acc.id)
            .order_by(PaperBotEvent.id.desc())
            .limit(limit)
        )
    ]
    risk = [
        {
            "time": _iso(e.occurred_at),
            "type": "RISK",
            "kind": e.kind,
            "reason": e.kind,
            "detail": e.detail,
        }
        for e in session.scalars(
            select(PaperRiskEvent)
            .where(PaperRiskEvent.account_id == acc.id)
            .order_by(PaperRiskEvent.id.desc())
            .limit(limit)
        )
    ]
    return sorted(bot + risk, key=lambda x: str(x["time"] or ""), reverse=True)[:limit]


def equity_curve(session: Session, acc: PaperAccount) -> dict[str, Any]:
    snaps = list(
        session.scalars(
            select(PaperEquitySnapshot)
            .where(PaperEquitySnapshot.account_id == acc.id)
            .order_by(PaperEquitySnapshot.at)
        )
    )
    points = [[_iso(s.at), _s(s.equity), _s(s.unrealized)] for s in snaps]
    daily: dict[str, Decimal] = {}
    for s in snaps:
        daily[(_utc(s.at) or s.at).date().isoformat()] = s.equity
    days = sorted(daily)
    prev = acc.initial_equity
    bars = []
    for d in days:
        bars.append([d, _s(daily[d] - prev)])
        prev = daily[d]
    return {"points": points, "daily_pnl": bars, "initial_equity": _s(acc.initial_equity, 2)}
