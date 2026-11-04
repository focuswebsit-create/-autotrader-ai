# Status

**Current phase:** Phase 3 — Autonomous PAPER Trading Engine & Live Terminal (built, PR awaiting review; results need weeks on persistent infrastructure) · **Mode:** PAPER ONLY · **Updated:** 2026-11-04

Phase 1 details and evidence: `docs/PHASE1_REPORT.md`.

## Phase 3 — PAPER engine & terminal (see PHASE3_REPORT.md)
| Gate item | State |
|---|---|
| Autonomous LONG/SHORT on live L2 data, fees / slippage / funding, SL/TP | ✅ engine + tests; live run on mainnet data |
| Risk limits 0.25% / 1% daily / 5% DD, 3-loss halt, max 4/day | ✅ engine + DB guards + tests |
| No duplicate / retroactive trades, restart recovery | ✅ unique keys, entry window, DB real-time guards, tests |
| Ledger / positions / trades in DB, append-only | ✅ migration 0009, PostgreSQL tests |
| `/terminal` (Dark Premium, RTL, desktop + iPhone), start gating, stop, Kill Switch | ✅ E2E on the live stack |
| Control API: login, ADMIN, CSRF, rate limit, audit log, double-start protection | ✅ 22 tests + PostgreSQL concurrency test |
| Independent code & security review | ✅ 8 findings, all fixed with regression tests |
| Docker services with auto-restart and monitoring | ✅ config validated; images built by CI only |
| Profitability | ❌ not shown — no closed paper trades yet; experimental |

## Phase 0 gate: "startup performs public, read-only discovery and proves it cannot trade"

| Item | State | Evidence |
|---|---|---|
| Exact `xyz:CL` market verified from runtime metadata | ✅ | live test + worker run, see TEST_RESULTS.md |
| Source availability (candles 30m / 1h) | ✅ | 5,005 × 30m (≈104 d), 5,003 × 1h (≈208 d) |
| No live-trading path, `/exchange` blocked, no keys accepted | ✅ | `tests/unit/test_safety*.py`, `autotrader safety-check` |
| Repo skeleton, Docker Compose, `.env.example` | ✅ | `docker compose config` valid (images built by the CI `docker` job) |
| PostgreSQL migrations (upgrade/downgrade) | ✅ | `tests/integration/test_postgres.py` on PostgreSQL 16 |
| Bootable API + worker + frontend | ✅ | end-to-end run + iPhone screenshot `docs/screenshots/phase0-iphone.png` |
| CI (ruff, mypy, pytest + Postgres, web lint/typecheck/build) | ✅ locally | `.github/workflows/ci.yml` — first GitHub run happens on the PR |
| External WTI reference session calendar | ⚠️ implemented, UNVERIFIED | `strategy/sessions.py` (ADR-025); official CME calendar not reachable from this environment |
| Documented fee behaviour for HIP-3 | ⚠️ partial | docs site blocked from this environment; see ADR-005 |

## QA review (2026-11-03)
Full review and fixes: see `docs/QA_REVIEW_PHASE0.md`. Locally: 178 tests passed (incl. 10 PostgreSQL + 3 live mainnet), coverage 97%.

## Known limitations
- `hyperliquid.gitbook.io` is blocked by the environment network policy, so ADR-004 and ADR-005 rest on prior knowledge plus live metadata. To re-verify, add that domain to the allowed list.
- Docker images cannot be built in the cloud dev container (no daemon); the CI `docker` job builds both images.
- (Phase 3) Bot control requires an ADMIN login (Control API, ADR-034). Read-only pages still have no login: keep the site behind Tailscale / an access layer.
- (Resolved in Phase 1) Least-privilege DB users `autotrader_api` / `autotrader_worker` (ADR-019).
- (Resolved in Phase 1) WebSocket client guarded by an allow-list equivalent to the HTTP one (ADR-018).
- Entry gating in the live API is still a placeholder (`entries_allowed=false`); the Phase 2 session/risk gates are used by the backtester and will drive the Phase 3 paper engine.

## Phase 1 — Market Data Engine ✅ (see PHASE1_REPORT.md)
| Gate item | State |
|---|---|
| 30m/1h backfill within the API cap, with coverage report | ✅ 5,008 / 5,004 closed bars, 0 missing |
| Persistent WS worker: ping, stale detection, reconnect with backoff, REST gap fill | ✅ verified live (isolated 60 s outage) and in tests |
| Restart / reconnect without loss or duplicates | ✅ live + tests |
| Gap / duplicate / OHLC / outlier validation, recorded not hidden | ✅ |
| Closed-bar-only reads (no look-ahead) | ✅ `closed_candles(as_of)` |
| Provenance for all observations | ✅ `ingestion_runs` |
| Health flags (stale data, WS state, heartbeat) | ✅ `/api/collector/status` |

## Phase 2 — Backtesting & Strategy Engine ✅ (see PHASE2_REPORT.md)
| Gate item | State |
|---|---|
| Deterministic results (same data + code ⇒ same report sha256) | ✅ tested |
| No look-ahead (indicator/decision prefix invariance, truncated-future engine test, auditor) | ✅ tested on real data |
| Next-bar execution, spread, adverse slippage, fees, recorded funding | ✅ |
| Conservative ambiguous bars (stop first) + sensitivity | ✅ |
| CME session / holidays / roll gate, fail closed | ✅ (calendar UNVERIFIED) |
| 70/15/15 split, dev-only optimisation, walk-forward, stress tests | ✅ |
| INCONCLUSIVE when the sample can't prove an edge | ✅ |
| Hebrew Backtest Lab (chart with entries/exits, equity, KPIs, trades) | ✅ `/backtest` |

Results after the pre-merge research audit (2026-11-04, 104 days of 30m data, observed fee 0.009%): EMA 20/50+RSI **FAIL**, Trend Following **INCONCLUSIVE**, Breakout+Volume **INCONCLUSIVE** (break-even). No strategy shows an edge after costs. The holdout of this dataset is EXPOSED: no untouched independent test exists until forward data after 2026-11-04 08:00 UTC accumulates (ADR-029).

## Phase 2.5 — Forward Testing & Strategy Validation ✅ built (see PHASE2_5_REPORT.md)
| Item | State |
|---|---|
| Strategies + params + costs + risk + code hash frozen before the test | ✅ immutable `forward_tests` |
| Real-time per-bar decision journal (timestamp, market data, version, reason) | ✅ append-only `forward_decisions` |
| No retroactive changes (DB triggers, results recomputed, code-change halt) | ✅ tested on PostgreSQL |
| Virtual P&L with fees/spread/slippage/funding (same engine as backtest) | ✅ |
| Backtest vs forward comparison (parity + consistency) | ✅ |
| INCONCLUSIVE until 30 trades / 30 days / 15 trade days | ✅ |
| Dashboard `/forward` | ✅ |
| 30+ days of forward observation | ⏳ needs the `forward` service running continuously (Docker Compose) |

## Next: Phase 3 — Autonomous forward paper engine (NOT started; awaiting owner approval)
