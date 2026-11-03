"""Background worker entrypoint: ``python -m autotrader.worker``.

Phase 0: runs safety checks and read-only market discovery, optionally on an
interval. Later phases add candle ingestion and the paper engine.
"""

from __future__ import annotations

import argparse
import logging
import time

from autotrader.common.config import get_settings
from autotrader.common.http import InfoClient
from autotrader.common.logging import configure_logging
from autotrader.db.session import session_factory
from autotrader.worker.discovery_job import run_discovery_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader-worker")
    parser.add_argument(
        "--interval", type=int, default=0, help="seconds between discoveries (0 = run once)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    sessions = session_factory(settings.database_url) if settings.database_url else None
    if sessions is None:
        logging.getLogger("autotrader.worker").warning("no database configured; not persisting")

    with InfoClient() as client:
        while True:
            outcome = run_discovery_once(settings, client, sessions)
            if args.interval <= 0:
                return 0 if outcome.ok else 2
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
