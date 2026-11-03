# QA Review — Phase 0 (2026-11-03)

Scope: the whole PR #1 (backend, frontend, DB, CI, Docker). Two independent passes:
1. Hands-on system testing (fault injection, live mainnet, browser rendering).
2. A separate reviewer agent, briefed without the author's conclusions, that tried to break the safety layer and data handling.

Every finding below was reproduced or verified by running code, unless marked otherwise.

## Trading safety (PAPER-only)

| Check | Result |
|---|---|
| `/exchange` reachable via URL tricks (encoding, `..`, `//`, `;`, `#`, `?`, userinfo, `\@`, IDNA, IPv6, ports, trailing dot, case) | **Blocked**. Verified with an httpx-normalised URL matrix. |
| Redirect to `/exchange` | **Not followed**. Test: 301/302/307/308 → only `/info` was sent. |
| Other HTTP stacks in source (`requests`, `urllib.request`, `socket`, `websockets`, `from httpx import`, dynamic import, `"/ex"+"change"`) | **Forbidden by static test**. Mutation-tested: a planted file makes it fail. |
| Network access from non-live tests | **Blocked at socket level** (localhost only). |
| Signing libraries (`eth-account`, `web3`, Hyperliquid SDK) in deps/lockfile/imports | None |
| Wallet/key env vars | Names (normalised, ~25 variants) **and values** (64-hex keys, 12–24-word mnemonics) refuse startup. The value is never echoed. |
| Non-PAPER mode | Exact `PAPER` only, consistent with `Settings`. |
| API surface | GET only. POST → 405. No order/trade/wallet routes. |
| DB tripwires | `paper_only` can't be set false, deleted, renamed or truncated. `audit_log` is append-only, TRUNCATE included. Limitation: the table owner can still ALTER/DROP the guards (ADR-010). |

## Findings and fixes

| ID | Severity | Finding | Fix | Regression test |
|---|---|---|---|---|
| S1 | Medium | Worker crashed with a traceback when the DB was down; any exception ended `--interval` mode | Persistence errors are classified (outage → exit 3 / data rejected → recorded failure); the loop survives everything except a safety violation | `test_resilience.py` |
| S2 | Medium | `/health` returned 200 while the DB was down | 503 + body; UI still renders the real state | `test_health_is_503_when_db_down` |
| M1 | Medium | Extreme values (`1e40`) raised `decimal.InvalidOperation`; values too large for the DB failed on insert; neither was recorded, so old data looked current | Sanity bounds (ADR-011), `ArithmeticError` caught, unexpected errors and `DB_REJECTED` recorded as failures | `test_out_of_range_values_fail_closed`, `test_unexpected_error_is_recorded_as_failure`, `test_db_rejecting_data_is_recorded_as_failure`, `test_extreme_values_never_reach_the_database` (PG) |
| M2 | Medium | `paper_only` row could be renamed; `system_config`/`audit_log` could be truncated | Migration `0002_hardening` | `test_paper_only_row_cannot_be_renamed`, `test_guarded_tables_cannot_be_truncated` |
| M3 | Medium | Env denylist missed e.g. `HL_API_SECRET`, `PK`, `SIGNER_KEY`, `PRIVATE-KEY`, and raw hex keys under innocent names | Normalised name rules + value detection | `test_signing_material_in_env_refuses_startup`, `test_key_like_values_refuse_startup`, `test_ordinary_env_is_accepted` |
| M4 | Medium | The PG test fixture dropped all tables of whatever `AUTOTRADER_DATABASE_URL` pointed to (the dev DB in `.env.example`) | Dedicated `AUTOTRADER_TEST_DATABASE_URL`, DB name must end in `_test` | Verified: `autotrader_dev` → 10 skipped |
| M5 | Medium | "Failure after discovery" compared two clocks; with SQLite's 1 s precision a failure was hidden; events weren't filtered by market | `system_events.subject` + ordering by id; explicit `state` | `test_same_second_failure_is_surfaced_without_backdating`, `test_failure_for_other_coin_is_ignored`, `test_success_after_failure_is_current_again` |
| M6 | Medium | UI kept a green "active" badge and bare prices after a failure | `CURRENT/STALE/FAILED` badge, red alert, "last known" labels, data age, coin from the backend | Verified in a browser (screenshots in this review) |
| W1 | Low | No security headers on the web app | CSP, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy; `redirect: "error"` on fetch | Browser check: headers present, 0 console errors |
| W2 | Low | Open tab never refreshed | Refresh every 60 s + render time shown | — |
| L2 | Low | `midPx ≤ 0` accepted as ACTIVE; negative OI shown as "zero"; duplicate dex silently used; `"20"`/`true` coerced | DEGRADED / OUT_OF_RANGE / DEX_AMBIGUOUS / strict types | `test_discovery.py` |
| L4 | Low | Lenient mode check (`"paper"`) disagreed with `Settings` | Exact match | `test_mode_check_is_exact_like_settings` |
| L7 | Low | No `Retry-After`, unbounded response size, URL userinfo / stray `?` allowed | Retry-After (capped 30 s), streaming 16 MB cap, rejected | `test_info_client_*` |
| C1 | Low | CI: Node 20 deprecation; Postgres healthcheck queried a non-existent DB; push+PR double runs | checkout v6, setup-node v6, setup-uv v7 (all node24, verified from `action.yml`); `-d autotrader_test`; push only on `main` + concurrency | CI run on this PR |
| D1 | Low | Docker: uv 0.8.22 vs lockfile from uv 0.11.32; web image shipped devDependencies and was root-owned; no API healthcheck | uv 0.11.32; Next `standalone` output, `--chown=node`; healthchecks; new CI `docker` job builds both images | CI `docker` job |
| X1 | Low | 0002 backfill left `subject` empty for 0001-era success events | Backfill from message prefix | Verified on a DB with real 0001 data |
| X2 | Low | My own interim commit `266e49b` contained a ruff error (the exit code was hidden by a pipe) | Fixed in `3fe15ff`; checks now run with `pipefail` | CI |

## Not verified / out of reach in this environment
- Docker images can't be built here (no daemon). The new CI `docker` job is the first real build.
- `hyperliquid.gitbook.io` is blocked, so the price-precision rule (ADR-004) and HIP-3 fee behaviour (ADR-005) are not re-verified against the docs.
- A WebSocket client doesn't exist yet. It must go through an equivalent allow-list (Phase 1 requirement).
