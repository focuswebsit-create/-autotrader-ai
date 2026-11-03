"""Background worker entrypoint: ``python -m autotrader.worker``.

Phase 0: runs safety checks and read-only market discovery, optionally on an
interval. Later phases add candle ingestion and the paper engine.

Exit codes (single run): 0 ok, 2 discovery failed (fail-closed), 3 result could
not be persisted (database unavailable). In interval mode the loop survives
transient failures; only a ``SafetyViolationError`` stops the process.
"""

from __future__ import annotations

import argparse
import logging
import time

from autotrader.common.config import get_settings
from autotrader.common.http import InfoClient
from autotrader.common.logging import configure_logging
from autotrader.common.safety import SafetyViolationError
from autotrader.db.session import session_factory
from autotrader.worker.discovery_job import DiscoveryOutcome, run_discovery_once

log = logging.getLogger("autotrader.worker")

EXIT_OK = 0
EXIT_DISCOVERY_FAILED = 2
EXIT_NOT_PERSISTED = 3
EXIT_UNEXPECTED = 4


def _exit_code(outcome: DiscoveryOutcome) -> int:
    if not outcome.ok:
        return EXIT_DISCOVERY_FAILED
    if not outcome.persisted:
        return EXIT_NOT_PERSISTED
    return EXIT_OK


def main(argv: list[str] | None = None, *, max_iterations: int | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autotrader-worker")
    parser.add_argument(
        "--interval", type=int, default=0, help="seconds between discoveries (0 = run once)"
    )
    args = parser.parse_args(argv)
    if args.interval < 0:
        parser.error("--interval must be >= 0")

    settings = get_settings()
    configure_logging(settings.log_level)
    sessions = session_factory(settings.database_url) if settings.database_url else None
    if sessions is None:
        log.warning("no database configured; not persisting")

    iteration = 0
    with InfoClient() as client:
        while True:
            iteration += 1
            try:
                code = _exit_code(run_discovery_once(settings, client, sessions))
            except SafetyViolationError:
                log.critical("safety violation - stopping worker")
                raise
            except Exception:
                # Fail closed: nothing is executed on error; log and retry next cycle.
                log.exception("unexpected error in discovery cycle")
                code = EXIT_UNEXPECTED
            if args.interval == 0:
                return code
            if max_iterations is not None and iteration >= max_iterations:
                return code
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
