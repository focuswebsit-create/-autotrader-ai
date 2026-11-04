"""Phase 2.5 forward testing: freezing, live recording, parity, integrity, results."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from autotrader.api.app import create_app
from autotrader.backtest.engine import ExitReason
from autotrader.common.config import Settings
from autotrader.db.market_data import upsert_candles
from autotrader.db.models import Base, ForwardDecision, ForwardEvent, ForwardTest, FundingRate
from autotrader.forward import evaluate as ev
from autotrader.forward import recorder as rec
from autotrader.forward.freeze import (
    Thresholds,
    canonical_sha256,
    costs_from_config,
    execution_from_config,
    next_bar_boundary,
    params_from_config,
    risk_from_config,
)
from autotrader.market.candles import Candle, Interval, parse_candle
from autotrader.strategy.base import Side
from autotrader.strategy.strategies import build_strategy
from tests.conftest import FIXTURES, PHASE2_RECORDED_AT
from tests.unit.bt_helpers import STEP, T0, D, Scripted, flat, run

COIN = "xyz:CL"
#: "today" inside the recorded data: ~2026-10-10, leaving ~25 days of "future"
FREEZE_AT = datetime(2026, 10, 10, 9, 7, tzinfo=UTC)
H10 = datetime(2026, 10, 10, 10, tzinfo=UTC)  # first hour fully inside the test


def _gz(name: str) -> Any:
    with gzip.open(FIXTURES / name, "rt") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> sessionmaker[Session]:
    """SQLite with the real recorded xyz:CL candles and funding (closed bars only)."""
    path: Path = tmp_path_factory.mktemp("fwd") / "fwd.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for name, iv in (
        ("candleSnapshot_xyzCL_30m_2026-11-04_full.json.gz", Interval.M30),
        ("candleSnapshot_xyzCL_1h_2026-11-04_full.json.gz", Interval.H1),
    ):
        cs = [parse_candle(r, coin=COIN, interval=iv) for r in _gz(name)]
        ok = [c for c in cs if isinstance(c, Candle) and c.is_closed_at(PHASE2_RECORDED_AT)]
        with sessions.begin() as s:
            upsert_candles(s, ok, source="rest", now=PHASE2_RECORDED_AT)
    with sessions.begin() as s:
        for r in _gz("fundingHistory_xyzCL_2026-11-04.json.gz"):
            t = datetime.fromtimestamp(r["time"] / 1000, UTC)
            s.add(
                FundingRate(
                    coin=COIN,
                    time=t,
                    hour=t.replace(minute=0, second=0, microsecond=0),
                    rate=Decimal(r["fundingRate"]),
                    premium=None,
                    source="fixture",
                    received_at=t,
                )
            )
    return sessions


def _fresh_copy(db: sessionmaker[Session], tmp_path: Path) -> sessionmaker[Session]:
    """Per-test copy of the loaded database (tests that create forward tests)."""
    import shutil

    src = Path(str(db.kw["bind"].url.database))
    dst = tmp_path / "copy.db"
    shutil.copy(src, dst)
    return sessionmaker(bind=create_engine(f"sqlite:///{dst}", poolclass=NullPool),
                        expire_on_commit=False)  # fmt: skip


def _run_live(
    sessions: sessionmaker[Session], test_id: int, until: datetime, step: timedelta
) -> list[rec.TickResult]:
    out = []
    now = FREEZE_AT
    while now < until:
        now = min(now + step, until)
        with sessions.begin() as s:
            out.append(rec.tick(s, s.get(ForwardTest, test_id), now=now))
    return out


# --------------------------------------------------------------------------- freezing


def test_freeze_fixes_config_and_starts_in_the_future(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        t = rec.create_test(s, COIN, "breakout_volume", now=FREEZE_AT, code_version="test")
        tid = t.id
    with sessions() as s:
        t = s.get(ForwardTest, tid)
        assert t is not None
        starts = t.starts_at.replace(tzinfo=UTC)
        assert starts == datetime(2026, 10, 10, 9, 30, tzinfo=UTC) > FREEZE_AT
        assert t.config_sha256 == canonical_sha256(t.config)
        assert t.config["paper_only"] is True
        # every frozen object round-trips exactly
        p = params_from_config(t.config)
        assert p == build_strategy("breakout_volume").params
        assert costs_from_config(t.config).as_dict() == t.config["costs"]
        assert risk_from_config(t.config).as_dict() == t.config["risk"]
        assert execution_from_config(t.config).as_dict() == t.config["execution"]
        assert rec.test_state(s, tid) == "ACTIVE"
        assert [e.event for e in s.scalars(select(ForwardEvent))] == ["STARTED"]
    with sessions.begin() as s, pytest.raises(ValueError, match="already exists"):
        rec.create_test(s, COIN, "breakout_volume", now=FREEZE_AT, code_version="test")


def test_next_bar_boundary() -> None:
    assert next_bar_boundary(datetime(2026, 1, 1, 10, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, 10, 0, tzinfo=UTC
    )
    assert next_bar_boundary(datetime(2026, 1, 1, 10, 0, 1, tzinfo=UTC)) == datetime(
        2026, 1, 1, 10, 30, tzinfo=UTC
    )


def test_tampered_params_do_not_reproduce_hash(db: sessionmaker[Session], tmp_path: Path) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        t = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="test")
        cfg = json.loads(json.dumps(t.config))
    cfg["params"]["fast"] = 21
    with pytest.raises(ValueError, match="hash"):
        params_from_config(cfg)


# --------------------------------------------------------------------------- recording


@pytest.fixture(scope="module")
def live(db: sessionmaker[Session], tmp_path_factory: pytest.TempPathFactory) -> Any:
    """breakout_volume forward-tested over ~20 days, ticking every 5h (irregular cadence)."""
    sessions = _fresh_copy(db, tmp_path_factory.mktemp("live"))
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "breakout_volume", now=FREEZE_AT, code_version="t").id
    until = FREEZE_AT + timedelta(days=20, minutes=3)
    ticks = _run_live(sessions, tid, until, timedelta(hours=5, minutes=7))
    return sessions, tid, until, ticks


def test_records_one_decision_per_closed_bar_after_start_only(live: Any) -> None:
    sessions, tid, until, ticks = live
    with sessions() as s:
        t = s.get(ForwardTest, tid)
        rows = list(s.scalars(select(ForwardDecision).order_by(ForwardDecision.bar_open_time)))
    starts = t.starts_at.replace(tzinfo=UTC)
    opens = [r.bar_open_time.replace(tzinfo=UTC) for r in rows]
    assert opens[0] == starts
    assert opens == sorted(set(opens))  # unique, chronological
    # every bar from start until the last closed one, no more (20 days x 48 bars)
    assert len(rows) == int((until - starts) / STEP)
    for r in rows:
        assert r.evaluated_at.replace(tzinfo=UTC) >= r.bar_close_time.replace(tzinfo=UTC)
        assert r.code_hash == t.code_hash and r.params_hash == t.params_hash
        assert r.market["bar"]["close"] and len(r.input_digest) == 64
    assert sum(x.signals for x in ticks) == sum(1 for r in rows if r.side)
    assert sum(x.signals for x in ticks) > 0
    # ticking every 5h means most decisions came in a batch after their bar closed
    assert any(r.late for r in rows) and any(not r.late for r in rows)


def test_tick_is_idempotent(live: Any) -> None:
    sessions, tid, until, _ = live
    with sessions.begin() as s:
        again = rec.tick(s, s.get(ForwardTest, tid), now=until)
    assert again.recorded == 0


def test_live_decisions_equal_full_history_recompute(live: Any, recorded_market: Any) -> None:
    """The live journal is exactly what the backtest code computes on the same bars:
    tick cadence and later data never change a decision (no look-ahead, reproducible)."""
    sessions, tid, _, _ = live
    with sessions() as s:
        t = s.get(ForwardTest, tid)
        rows = list(s.scalars(select(ForwardDecision).order_by(ForwardDecision.bar_open_time)))
        anchor = t.history_anchor.replace(tzinfo=UTC)
    m30 = [b for b in recorded_market.m30 if b.open_time >= anchor]  # ALL data incl. future
    h1 = [b for b in recorded_market.h1 if b.open_time >= anchor]
    prepared = build_strategy("breakout_volume").prepare(m30, h1)
    idx = {b.open_time: i for i, b in enumerate(m30)}
    for r in rows:
        d = prepared.decide(idx[r.bar_open_time.replace(tzinfo=UTC)])
        assert (d.side.value if d.side else None, d.reason, d.atr) == (r.side, r.reason, r.atr)


def test_report_is_recomputed_valid_and_inconclusive(live: Any) -> None:
    sessions, tid, until, _ = live
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=until)
        again = ev.evaluate(s, s.get(ForwardTest, tid), now=until)
    assert json.dumps(r, sort_keys=True, default=str) == json.dumps(
        again, sort_keys=True, default=str
    )
    assert r["paper_only"] is True
    assert r["parity"]["mismatches"] == 0 and r["parity"]["checked"] == r["progress"]["decisions"]
    assert r["audit"]["violation_count"] == 0
    assert r["test"]["code_hash_matches"] is True
    v = r["verdict"]
    assert v["status"] == "INCONCLUSIVE"  # 20 days < 30 required, few trades
    assert any("not enough" in x for x in v["reasons"])
    s_ = r["summary"]
    assert s_["trades"] == len(r["trades"]) == r["progress"]["closed_trades"]
    for t in r["trades"]:
        assert t["entry_time"] >= r["test"]["starts_at"]  # nothing from before the freeze
    assert r["comparison"]["available"] is False  # no reference run stored in this DB


def test_results_never_use_bars_after_the_last_recorded_decision(live: Any) -> None:
    sessions, tid, until, _ = live
    with sessions() as s:
        later = ev.evaluate(s, s.get(ForwardTest, tid), now=until + timedelta(days=3))
        now_r = ev.evaluate(s, s.get(ForwardTest, tid), now=until)
    # bars closed later exist in the DB but weren't decided: results are unchanged
    assert later["trades"] == now_r["trades"]
    assert later["progress"]["last_decision_bar"] == now_r["progress"]["last_decision_bar"]


def test_tampered_journal_is_detected(db: sessionmaker[Session], tmp_path: Path) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
    _run_live(sessions, tid, FREEZE_AT + timedelta(days=2), timedelta(hours=12))
    with sessions.begin() as s:
        # SQLite has no immutability trigger: simulate a forged edit
        row = s.scalars(select(ForwardDecision).order_by(ForwardDecision.id)).first()
        assert row is not None
        row.side, row.reason, row.atr = "LONG", "SIGNAL", Decimal(1)
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(days=2))
    assert r["parity"]["mismatches"] == 1
    assert r["verdict"]["status"] == "INVALID"


def test_code_change_halts_the_test(
    db: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "trend_pullback", now=FREEZE_AT, code_version="t").id
    _run_live(sessions, tid, FREEZE_AT + timedelta(days=1), timedelta(hours=6))
    monkeypatch.setattr(rec, "strategy_code_hash", lambda: "0" * 64)
    monkeypatch.setattr(ev, "strategy_code_hash", lambda: "0" * 64)
    with sessions.begin() as s:
        n_before = s.scalar(select(func.count()).select_from(ForwardDecision))
        res = rec.tick(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(days=2))
    assert res.state == "INTEGRITY_HALT" and res.recorded == 0
    with sessions.begin() as s:
        assert rec.test_state(s, tid) == "INTEGRITY_HALT"
        assert rec.tick(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(days=3)).recorded == 0
        assert s.scalar(select(func.count()).select_from(ForwardDecision)) == n_before
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(days=3))
    assert r["verdict"]["status"] == "INVALID"


def test_stop_is_final_and_new_test_allowed(db: sessionmaker[Session], tmp_path: Path) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
        rec.stop_test(s, tid, reason="owner request", now=FREEZE_AT + timedelta(hours=1))
    with sessions.begin() as s:
        assert rec.tick(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(days=1)).recorded == 0
        with pytest.raises(ValueError):
            rec.stop_test(s, tid, reason="again", now=FREEZE_AT + timedelta(days=1))
        # a fresh, separately frozen test may start afterwards (never a resumed old one)
        assert rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT + timedelta(days=1),
                               code_version="t").id != tid  # fmt: skip


def test_data_gap_and_stale_are_recorded(db: sessionmaker[Session], tmp_path: Path) -> None:
    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
        # remove two closed 30m bars inside the test period (outage at the exchange/collector)
        from autotrader.db.models import CandleRow

        for k in (3, 4):
            t0 = datetime(2026, 10, 10, 9, 30, tzinfo=UTC) + k * STEP
            s.query(CandleRow).filter(
                CandleRow.interval == "30m", CandleRow.open_time == t0
            ).delete()
    _run_live(sessions, tid, FREEZE_AT + timedelta(hours=6), timedelta(hours=6))
    with sessions() as s:
        evs = [
            (e.event, e.detail) for e in s.scalars(select(ForwardEvent).order_by(ForwardEvent.id))
        ]
    gaps = [d for e, d in evs if e == "DATA_GAP"]
    assert gaps and gaps[0]["bars"] == 2
    # data feed stops: an evaluation 2h after the last closed bar flags it as stale
    with sessions.begin() as s:
        res = rec.tick(s, s.get(ForwardTest, tid), now=datetime(2026, 11, 4, 11, tzinfo=UTC))
    assert any("stale" in n for n in res.notes)


# --------------------------------------------------------------------------- engine live edge


def test_live_edge_keeps_position_open_and_signal_pending() -> None:
    from autotrader.backtest.engine import Engine, ExecConfig, FundingBook, RiskConfig
    from tests.unit.bt_helpers import NO_COSTS

    b = flat(6)
    ex = ExecConfig(require_funding_data=False)
    res = Engine(b, Scripted({1: Side.LONG}), stop_atr=D("1.5"), tp_r=D(2), max_hold_bars=None,
                 costs=NO_COSTS, risk=RiskConfig(), execution=ex, funding=FundingBook({}),
                 live_edge=True).run()  # fmt: skip
    assert res.trades == [] and res.open_trade is not None
    assert res.open_trade.exit_reason is None and res.open_trade_mark is not None
    # same data as a closed backtest window: forced END_OF_DATA exit instead
    closed, viol = run(b, {1: Side.LONG}, costs=NO_COSTS)
    assert viol == [] and closed.trades[0].exit_reason is ExitReason.END_OF_DATA
    # a signal on the very last bar waits for the next bar
    pend = Engine(b, Scripted({5: Side.SHORT}), stop_atr=D("1.5"), tp_r=D(2), max_hold_bars=None,
                  costs=NO_COSTS, risk=RiskConfig(), execution=ex, funding=FundingBook({}),
                  live_edge=True).run()  # fmt: skip
    assert pend.pending is not None and pend.pending.status == "PENDING"
    assert pend.pending.signal_time == T0 + 6 * STEP


# --------------------------------------------------------------------------- verdict


def test_forward_verdict_rules() -> None:
    th = Thresholds()
    base: dict[str, Any] = {"th": th, "code_ok": True, "parity_ok": True, "violations": 0,
                            "state": "ACTIVE"}  # fmt: skip
    ok = {"n": 40, "observed_days": 45, "trade_days": 20}
    assert ev.forward_verdict(ci=[0.1, 0.6], stress_net="5", **ok, **base)["status"] == "PASS"
    assert ev.forward_verdict(ci=[-0.6, -0.1], stress_net="-5", **ok, **base)["status"] == "FAIL"
    assert (
        ev.forward_verdict(ci=[-0.2, 0.3], stress_net="1", **ok, **base)["status"] == "INCONCLUSIVE"
    )
    few = ev.forward_verdict(ci=[0.5, 0.9], stress_net="9", n=12, observed_days=45, trade_days=10,
                             **base)  # fmt: skip
    assert few["status"] == "INCONCLUSIVE"  # strongly positive but too few observations
    clustered = ev.forward_verdict(ci=[0.5, 0.9], stress_net="9", n=40, observed_days=45,
                                   trade_days=3, **base)  # fmt: skip
    assert clustered["status"] == "INCONCLUSIVE"
    bad = base | {"parity_ok": False}
    assert ev.forward_verdict(ci=[0.1, 0.6], stress_net="5", **ok, **bad)["status"] == "INVALID"


# --------------------------------------------------------------------------- API


def test_forward_api_is_read_only(live: Any) -> None:
    sessions, tid, until, _ = live
    from tests.conftest import Clock

    c = TestClient(create_app(Settings(database_url=None), sessions, clock=Clock(until)))
    tests = c.get("/api/forward/tests").json()["tests"]
    assert [t["id"] for t in tests] == [tid] and tests[0]["state"] == "ACTIVE"
    rep = c.get(f"/api/forward/tests/{tid}").json()
    assert rep["verdict"]["status"] == "INCONCLUSIVE" and rep["parity"]["mismatches"] == 0
    sig = c.get(f"/api/forward/tests/{tid}/decisions").json()["decisions"]
    assert sig and all(d["side"] in ("LONG", "SHORT") for d in sig)
    every = c.get(f"/api/forward/tests/{tid}/decisions", params={"signals_only": "false",
                                                                 "limit": 5}).json()  # fmt: skip
    assert len(every["decisions"]) == 5
    assert c.get("/api/forward/tests/9999").status_code == 404
    assert c.post("/api/forward/tests").status_code == 405
    assert c.post(f"/api/forward/tests/{tid}").status_code == 405


def test_reference_backtest_is_frozen_and_compared(
    db: sessionmaker[Session], tmp_path: Path, recorded_market: Any
) -> None:
    """The latest backtest of the SAME params is frozen into the test as the expectation."""
    from autotrader.backtest.research import MarketData, Runner
    from autotrader.backtest.store import save_run

    sessions = _fresh_copy(db, tmp_path)
    # a minimal stored report for exactly these parameters (as `autotrader backtest` saves)
    strat = build_strategy("breakout_volume")
    pre = MarketData(COIN, [b for b in recorded_market.m30 if b.close_time <= FREEZE_AT],
                     [b for b in recorded_market.h1 if b.close_time <= FREEZE_AT],
                     recorded_market.funding, "fixture", FREEZE_AT)  # fmt: skip
    out = Runner(pre).run("breakout_volume", strat.params, start=pre.m30[200].close_time)
    from autotrader.strategy.base import params_hash

    report = {
        "strategy": {"id": "breakout_volume", "version": strat.version, "title_he": "x"},
        "params_hash": params_hash("breakout_volume", strat.version, strat.params),
        "params": strat.params.as_dict(),
        "code": {"commit": "ref"},
        "data": {"coin": COIN, "fingerprint_sha256": pre.fingerprint()},
        "trade_window": {"start": pre.m30[200].close_time.isoformat(),
                         "end": pre.m30[-1].close_time.isoformat()},
        "verdict": {"status": "INCONCLUSIVE", "reasons": []},
        "primary": {"summary": out.summary, "trades": [],
                    "decision_counts": out.result.decision_counts},
        "report_sha256": "r" * 64,
    }  # fmt: skip
    with sessions.begin() as s:
        save_run(s, report, now=FREEZE_AT - timedelta(hours=1))
        tid = rec.create_test(s, COIN, "breakout_volume", now=FREEZE_AT, code_version="t").id
    _run_live(sessions, tid, FREEZE_AT + timedelta(days=10), timedelta(hours=8))
    with sessions() as s:
        t = s.get(ForwardTest, tid)
        ref = t.config["reference_backtest"]
        r = ev.evaluate(s, t, now=FREEZE_AT + timedelta(days=10))
    assert ref["summary"]["trades"] == out.summary["trades"] and ref["trades_per_day"] > 0
    cmp_ = r["comparison"]
    assert cmp_["available"] and cmp_["reference_run_id"] == ref["run_id"]
    metrics = {m["metric"]: m for m in cmp_["metrics"]}
    assert metrics["expectancy_r"]["backtest"] == out.summary["expectancy_r"]
    assert metrics["trades_per_day"]["forward"] is not None
    # fewer than 10 forward trades: no consistency claim either way
    assert r["stats"]["trades"] < 10 and cmp_["expectancy_consistency"] == "INSUFFICIENT_DATA"


def test_worker_records_for_active_tests_and_survives_funding_errors(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    import threading

    from autotrader.forward.worker import run_forever, tick_all

    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        a = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
        b = rec.create_test(s, COIN, "trend_pullback", now=FREEZE_AT, code_version="t").id
        rec.stop_test(s, b, reason="x", now=FREEZE_AT)
    res = tick_all(sessions, COIN, FREEZE_AT + timedelta(hours=3))
    assert [r.test_id for r in res] == [a] and res[0].recorded == 5

    class Boom:
        calls = 0

        def info(self, payload: dict[str, Any]) -> Any:
            Boom.calls += 1
            raise RuntimeError("network down")

    stop = threading.Event()
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1  # 1 = start-up, 2 = the single loop iteration
        if calls["n"] >= 2:
            stop.set()
        return FREEZE_AT + timedelta(hours=6)

    run_forever(sessions, COIN, Boom(), clock=clock, stop=stop)
    assert Boom.calls == 1  # funding failure logged, recording continued
    with sessions() as s:
        n = s.scalar(
            select(func.count()).select_from(ForwardDecision).where(ForwardDecision.test_id == a)
        )
    assert n == 11


# --------------------------------------------------------------------------- review fixes


def test_on_the_hour_bar_waits_for_its_1h_bar(db: sessionmaker[Session], tmp_path: Path) -> None:
    """Review H1: the collector commits 30m and 1h separately. A decision on an hourly
    close must not be made before the 1h bar closing at the same instant is stored."""
    from autotrader.db.models import CandleRow

    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "breakout_volume", now=FREEZE_AT, code_version="t").id
        # the 1h bar 10:00-11:00 is "not yet closed" (still forming) at 11:00:20
        h = s.scalars(select(CandleRow).where(
            CandleRow.interval == "1h", CandleRow.open_time == H10
        )).one()  # fmt: skip
        h.is_closed = False
    at = datetime(2026, 10, 10, 11, 0, 20, tzinfo=UTC)
    with sessions.begin() as s:
        res = rec.tick(s, s.get(ForwardTest, tid), now=at)
    # 09:30 and 10:00 bars recorded; 10:30 (closes 11:00) waits for its 1h bar
    assert res.recorded == 2 and any("waiting for the 1h bar" in n for n in res.notes)
    with sessions.begin() as s:
        s.scalars(select(CandleRow).where(
            CandleRow.interval == "1h", CandleRow.open_time == H10
        )).one().is_closed = True  # fmt: skip
    with sessions.begin() as s:
        assert rec.tick(s, s.get(ForwardTest, tid), now=at + timedelta(seconds=30)).recorded == 1
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=at + timedelta(minutes=1))
    assert r["parity"]["mismatches"] == 0


def test_missing_1h_bar_is_waited_for_then_recorded_as_gap(
    db: sessionmaker[Session], tmp_path: Path
) -> None:
    from autotrader.db.models import CandleRow

    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
        s.query(CandleRow).filter(
            CandleRow.interval == "1h", CandleRow.open_time >= H10
        ).delete()  # fmt: skip
    with sessions.begin() as s:
        r1 = rec.tick(s, s.get(ForwardTest, tid), now=datetime(2026, 10, 10, 11, 5, tzinfo=UTC))
    assert r1.recorded == 2
    with sessions.begin() as s:
        r2 = rec.tick(s, s.get(ForwardTest, tid), now=datetime(2026, 10, 10, 11, 20, tzinfo=UTC))
        gaps = [
            e.detail
            for e in s.scalars(select(ForwardEvent).where(ForwardEvent.event == "DATA_GAP"))
        ]
    assert r2.recorded == 1 and any("h1_missing_at" in g for g in gaps)


def test_revised_input_data_is_detected(db: sessionmaker[Session], tmp_path: Path) -> None:
    """Review M5: a candle corrected after a decision used it invalidates the result."""
    from autotrader.db.models import CandleRow

    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
    _run_live(sessions, tid, FREEZE_AT + timedelta(hours=4), timedelta(hours=4))
    with sessions.begin() as s:
        bar = s.scalars(select(CandleRow).where(
            CandleRow.interval == "30m", CandleRow.open_time == H10
        )).one()  # fmt: skip
        bar.volume = bar.volume + 1  # SQLite: no immutability trigger, simulate a correction
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=FREEZE_AT + timedelta(hours=4))
    assert any(m.get("issue") == "INPUT_DATA_CHANGED" for m in r["parity"]["examples"])
    assert r["verdict"]["status"] == "INVALID"


def test_observed_days_count_recorded_bars_not_wall_clock(live: Any) -> None:
    """Review M2: waiting (collector down / test stopped) doesn't add observation days."""
    sessions, tid, until, _ = live
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=until + timedelta(days=40))
    assert r["progress"]["observed_days"] < 20.01
    assert (
        next(c for c in r["verdict"]["criteria"] if c["criterion"] == "min_days")["passed"] is False
    )


def test_stopped_test_can_never_pass() -> None:
    """Review M3: no optional stopping - a stopped/halted test can't PASS."""
    base: dict[str, Any] = {"th": Thresholds(), "code_ok": True, "parity_ok": True,
                            "violations": 0}  # fmt: skip
    ok = {"n": 40, "observed_days": 45, "trade_days": 20, "ci": [0.1, 0.6], "stress_net": "5"}
    assert ev.forward_verdict(state="ACTIVE", **ok, **base)["status"] == "PASS"
    stopped = ev.forward_verdict(state="STOPPED", **ok, **base)
    assert stopped["status"] == "INCONCLUSIVE" and "still running" in stopped["reasons"][0]


def test_signal_frequency_uses_backtest_definition(live: Any) -> None:
    """Review M4: forward signals/day counts signals evaluated while flat, like the backtest."""
    sessions, tid, until, _ = live
    with sessions() as s:
        r = ev.evaluate(s, s.get(ForwardTest, tid), now=until)
    engine_signals = sum(1 for e in r["engine_signals"])
    recorded = r["progress"]["signals"]
    assert engine_signals <= recorded
    days = r["progress"]["observed_days"]
    assert (
        round(engine_signals / days, 4)
        == ev._compare(
            {"summary": {}, "run_id": 1}, r["stats"], r["summary"], days, engine_signals
        )["metrics"][4]["forward"]
    )


def test_code_hash_covers_evaluation_code() -> None:
    from autotrader.forward.freeze import CODE_FILES

    for f in ("forward/evaluate.py", "backtest/validation.py", "forward/freeze.py",
              "market/models.py", "db/market_data.py"):  # fmt: skip
        assert f in CODE_FILES


# --------------------------------------------------------------------------- monitoring


def test_worker_heartbeat_and_pipeline_health(db: sessionmaker[Session], tmp_path: Path) -> None:
    import threading

    from autotrader.db.market_data import get_heartbeat
    from autotrader.forward.worker import SERVICE_NAME, run_forever
    from tests.conftest import Clock

    sessions = _fresh_copy(db, tmp_path)
    with sessions.begin() as s:
        tid = rec.create_test(s, COIN, "ema_rsi", now=FREEZE_AT, code_version="t").id
    app = create_app(
        Settings(database_url=None), sessions, clock=Clock(FREEZE_AT + timedelta(hours=3))
    )
    c = TestClient(app)
    h = c.get("/api/forward/health")
    assert h.status_code == 503
    codes = {a["code"] for a in h.json()["alerts"]}
    assert "FORWARD_WORKER_NOT_STARTED" in codes
    assert f"FORWARD_TEST_{tid}_NOT_RECORDING" in codes  # started 09:30, nothing recorded

    stop = threading.Event()
    calls = {"n": 0}

    def clock() -> datetime:
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        return FREEZE_AT + timedelta(hours=3)

    run_forever(sessions, COIN, None, clock=clock, stop=stop)
    with sessions() as s:
        hb = get_heartbeat(s, SERVICE_NAME)
        assert hb is not None and hb.state == "STOPPED" and hb.ws_state == "NOT_APPLICABLE"
    rep = c.get(f"/api/forward/tests/{tid}").json()
    assert rep["progress"]["data_origin"] == "LIVE_FORWARD"
    assert rep["progress"]["realtime_decisions"] + rep["progress"]["late_decisions"] == 5
    h2 = c.get("/api/forward/health").json()
    codes2 = {a["code"] for a in h2["alerts"]}
    assert "FORWARD_WORKER_DOWN" in codes2  # stopped worker is reported, not hidden
    assert f"FORWARD_TEST_{tid}_NOT_RECORDING" not in codes2
    assert h2["paper_only"] is True
