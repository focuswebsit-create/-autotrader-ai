"""Command line: ``autotrader discover`` / ``autotrader safety-check``."""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy.orm import Session, sessionmaker

from autotrader.common.config import Settings, get_settings
from autotrader.common.http import InfoClient
from autotrader.common.logging import configure_logging
from autotrader.common.safety import (
    ALLOWED_INFO_TYPES,
    INFO_URL,
    TRADING_MODE,
    assert_paper_mode,
    check_environment,
)
from autotrader.db.session import session_factory
from autotrader.ingest.rest import backfill
from autotrader.market.candles import Interval
from autotrader.reporting.data_report import build_data_report
from autotrader.worker.discovery_job import run_discovery_once


def _discover(args: argparse.Namespace) -> int:
    settings = get_settings()
    sessions = (
        session_factory(settings.database_url) if settings.database_url and not args.no_db else None
    )
    with InfoClient() as client:
        outcome = run_discovery_once(settings, client, sessions)
    if outcome.ok and outcome.discovery is not None:
        print(json.dumps(outcome.discovery.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if outcome.persisted else 3
    print(
        json.dumps({"ok": False, "code": outcome.error_code, "message": outcome.error_message}),
        file=sys.stderr,
    )
    return 2


def _safety_check(_: argparse.Namespace) -> int:
    check_environment()
    assert_paper_mode()
    print(
        json.dumps(
            {
                "trading_mode": TRADING_MODE,
                "allowed_endpoint": f"POST {INFO_URL}",
                "allowed_info_types": sorted(ALLOWED_INFO_TYPES),
                "exchange_endpoint": "BLOCKED",
                "wallet_keys": "NOT ACCEPTED",
            },
            indent=2,
        )
    )
    return 0


def _require_db() -> tuple[Settings, sessionmaker[Session]] | None:
    settings = get_settings()
    if not settings.database_url:
        print(json.dumps({"ok": False, "code": "DATABASE_NOT_CONFIGURED"}), file=sys.stderr)
        return None
    return settings, session_factory(settings.database_url)


def _backfill(args: argparse.Namespace) -> int:
    check_environment()
    assert_paper_mode()
    env = _require_db()
    if env is None:
        return 3
    settings, sessions = env
    intervals = [Interval(args.interval)] if args.interval else list(Interval)
    out = []
    ok = True
    with InfoClient() as client:
        for iv in intervals:
            r = backfill(sessions, client, coin=settings.market_coin, interval=iv)
            ok = ok and r.ok
            out.append(
                {
                    "interval": iv.value,
                    "ok": r.ok,
                    "received": r.received,
                    "inserted": r.inserted,
                    "updated": r.updated,
                    "unchanged": r.unchanged,
                    "rejected": r.rejected,
                    "first_open_time": r.first_open_time.isoformat() if r.first_open_time else None,
                    "last_open_time": r.last_open_time.isoformat() if r.last_open_time else None,
                    "gaps": r.gaps,
                    "error": r.error,
                }
            )
    print(json.dumps(out, indent=2))
    return 0 if ok else 2


def _data_report(_: argparse.Namespace) -> int:
    env = _require_db()
    if env is None:
        return 3
    settings, sessions = env
    with sessions() as s:
        report = build_data_report(s, settings.market_coin)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _collect(_: argparse.Namespace) -> int:
    from autotrader.collector_main import main as collector_main

    return collector_main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader", description="AutoTrader AI (PAPER ONLY)")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="read-only discovery of the target market")
    d.add_argument("--no-db", action="store_true", help="do not persist the result")
    d.set_defaults(func=_discover)
    sc = sub.add_parser("safety-check", help="print and verify PAPER-ONLY guarantees")
    sc.set_defaults(func=_safety_check)
    bf = sub.add_parser("backfill", help="fetch all available candle history via REST")
    bf.add_argument("--interval", choices=[i.value for i in Interval])
    bf.set_defaults(func=_backfill)
    dr = sub.add_parser("data-report", help="coverage, gaps and data-quality issues (JSON)")
    dr.set_defaults(func=_data_report)
    co = sub.add_parser("collect", help="run the live market-data collector")
    co.set_defaults(func=_collect)
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
