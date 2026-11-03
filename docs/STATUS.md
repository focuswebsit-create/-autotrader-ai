# Status

**Current phase:** Phase 0 — Discovery & scaffolding · **Mode:** PAPER ONLY · **Updated:** 2026-11-03

## Phase 0 gate: "startup performs public, read-only discovery and proves it cannot trade"

| Item | State | Evidence |
|---|---|---|
| Exact `xyz:CL` market verified from runtime metadata | ✅ | live test + worker run, see TEST_RESULTS.md |
| Source availability (candles 30m / 1h) | ✅ | 5,005 × 30m (≈104 d), 5,003 × 1h (≈208 d) |
| No live-trading path, `/exchange` blocked, no keys accepted | ✅ | `tests/unit/test_safety*.py`, `autotrader safety-check` |
| Repo skeleton, Docker Compose, `.env.example` | ✅ | `docker compose config` valid (images not built here: no Docker daemon in the dev container) |
| PostgreSQL migrations (upgrade/downgrade) | ✅ | `tests/integration/test_postgres.py` on PostgreSQL 16 |
| Bootable API + worker + frontend | ✅ | end-to-end run + iPhone screenshot `docs/screenshots/phase0-iphone.png` |
| CI (ruff, mypy, pytest + Postgres, web lint/typecheck/build) | ✅ locally | `.github/workflows/ci.yml` — first GitHub run happens on the PR |
| External WTI reference session calendar | ⏳ not started | Needed before Phase 3 entries; until then entries are blocked |
| Documented fee behaviour for HIP-3 | ⚠️ partial | docs site blocked from this environment; see ADR-005 |

## Known limitations
- `hyperliquid.gitbook.io` is blocked by the environment network policy, so ADR-004 and ADR-005 rest on prior knowledge plus live metadata. To re-verify, add that domain to the allowed list.
- Docker images haven't been built in the cloud dev container (no daemon). The compose file is validated syntactically only.
- No authentication yet (read-only API, localhost-bound). Required before any online exposure (Phase 4).
- Entry gating is a placeholder (`entries_allowed=false`, reason `PHASE_0_NO_STRATEGY`).

## Next: Phase 1 — ingestion and quality
30m/1h backfill within the 5k cap with coverage report; `candles` table with provenance; persistent WS worker (ping, reconnect, REST backfill); gap/duplicate/OHLC validation; stale-data and oracle/mark health flags.
