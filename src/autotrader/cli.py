"""Command line: ``autotrader discover`` / ``autotrader safety-check``."""

from __future__ import annotations

import argparse
import json
import sys

from autotrader.common.config import get_settings
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
        return 0
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader", description="AutoTrader AI (PAPER ONLY)")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="read-only discovery of the target market")
    d.add_argument("--no-db", action="store_true", help="do not persist the result")
    d.set_defaults(func=_discover)
    sc = sub.add_parser("safety-check", help="print and verify PAPER-ONLY guarantees")
    sc.set_defaults(func=_safety_check)
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
