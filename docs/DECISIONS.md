# Architecture Decision Log

Each entry: context → decision → consequences. Newest last.

## ADR-001 — PAPER-ONLY enforced in code, not configuration (2026-11-03)
**Context.** The blueprint forbids any working live-execution path, "even behind a feature flag".
**Decision.**
- `autotrader.common.safety` defines `TRADING_MODE = "PAPER"` as a constant. `Settings.trading_mode` is typed `Literal["PAPER"]`, so any other env value fails validation.
- Startup (`check_environment`) aborts if any env var name looks like signing material (`PRIVATE_KEY`, `MNEMONIC`, `SEED_PHRASE`, `AGENT_KEY`, `API_WALLET`, ...) or if `TRADING_MODE`/`AUTOTRADER_TRADING_MODE` is set to anything except `PAPER`.
- The DB pins `system_config.paper_only = true` with a CHECK constraint and a delete trigger.
**Consequences.** There is no code path to "turn on" live trading. A future live project would need a separate repository and review (blueprint §8).

## ADR-002 — Own read-only HTTP client instead of the official Hyperliquid Python SDK (2026-11-03, approved by owner)
**Context.** The official SDK bundles signing (`eth-account`) and order/transfer helpers.
**Decision.** A minimal `httpx` client (`autotrader.common.http.InfoClient`). Every request passes through `AllowlistTransport`, which validates **before sending**:
- method `POST`, URL exactly `https://api.hyperliquid.xyz/info` (no other host/port/path/query);
- JSON body with `type` in an allow-list (`perpDexs`, `meta`, `metaAndAssetCtxs`, `candleSnapshot`, `l2Book`, `fundingHistory`, `predictedFundings`, `allMids`);
- no signing fields (`signature`, `action`, `nonce`, `vaultAddress`).

Static tests fail the build if `eth-account`, `web3` or the Hyperliquid SDK appear in dependencies/lockfile/imports, if any module except `safety.py` contains an `/exchange` string literal, or if any module except `common/http.py` creates an httpx client.
**Consequences.** Adding a new Info request type requires an explicit allow-list change in one reviewed file.

## ADR-003 — Market resolved by name at runtime; no hard-coded asset ID (2026-11-03)
**Decision.** `discover_market()` calls `perpDexs` → finds dex `xyz` → calls `metaAndAssetCtxs` with `dex: "xyz"` → finds the exact name `xyz:CL`. The coin must be namespaced by the dex (native `CL` is rejected). Missing dex/market, duplicates, delisting, malformed data and non-positive prices raise `DiscoveryError` with a code (fail closed). Oracle/mark divergence above the limit (default 50 bps), a missing mid price or zero OI → status `DEGRADED` with warnings.
The asset index is stored for information only and is never used for anything executable.
**Observed on 2026-11-03 (mainnet):** `perpDexs` = `[native, xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io]`. `xyz:CL` is at dex index 1, universe index 29; `szDecimals=3`, `maxLeverage=20`, `growthMode=enabled`, `deployerFeeScale=1.0`, mark ≈ oracle ≈ 95 USD.

## ADR-004 — Price precision rule (2026-11-03)
**Decision.** Use the documented perp rule: price ≤ 5 significant figures and ≤ `6 − szDecimals` decimals (= 3 for `xyz:CL`). Stored as `max_price_decimals`. **To verify:** the docs site (`hyperliquid.gitbook.io`) is blocked by this environment's network policy, so the rule comes from prior knowledge of the documentation and has not been re-checked against the live page. Order-size rounding is Phase 2/3 work and must re-verify it.

## ADR-005 — Fees: keep the blueprint's conservative assumption (2026-11-03)
**Context.** HIP-3 markets have deployer-scaled fees (`deployerFeeScale`, `growthMode`). The fee docs page could not be reached from this environment.
**Decision.** Phase 0 records `deployerFeeScale` and `growthMode` from metadata but doesn't derive a fee from them. Phase 2 uses the blueprint baseline **0.10% taker per fill + 0.05% adverse slippage per side**, labelled as a model assumption, with 0.5x/1x/2x stress.

## ADR-006 — Historical depth is limited (2026-11-03)
**Observed live:** `candleSnapshot` for `xyz:CL` returns 5,005 × 30m candles (≈104.2 days) and 5,003 × 1h candles (≈208.4 days). Results longer than that need a separately sourced, disclosed dataset. Phase 2 reports must say so explicitly.

## ADR-007 — Repository layout deviation (2026-11-03)
**Context.** The blueprint lists `src/market`, `src/strategy`, ..., `services/api`, `services/worker`.
**Decision.** Single installable Python package `src/autotrader/{common,market,db,api,worker,strategy,risk,paper,backtest,reporting}` (standard src-layout, one `pyproject.toml`, one lockfile). The "services" are entrypoints of the same package (`uvicorn autotrader.api.main:app`, `python -m autotrader.worker`) built from `infra/docker/backend.Dockerfile`. The frontend lives in `apps/web`.
**Consequences.** API and worker share exactly the same code (important for "same function in backtest and forward paper").

## ADR-008 — Tooling (2026-11-03)
Python ≥3.12 (CI on 3.12) with `uv` + `uv.lock`; FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, psycopg 3, httpx; ruff + mypy `--strict` + pytest. PostgreSQL 16. Next.js 16 (App Router, server-rendered, `force-dynamic` so no fake static state) + React 19 + Tailwind 4 + ESLint 9; Hebrew RTL, light/dark, system fonts (no build-time font downloads). Redis not used. No auth yet: the API is read-only and bound to `127.0.0.1` in compose. Auth is a Phase 4 requirement before any online exposure.

## ADR-009 — Proxy-aware transport (2026-11-03)
httpx ignores proxy env vars when a custom transport is used. `_default_inner_transport()` resolves `HTTPS_PROXY`/`NO_PROXY` explicitly (CA bundle via `SSL_CERT_FILE`). The allow-list check still runs first.

## ADR-010 — Database guards are tripwires; runtime role separation is deferred (QA 2026-11-03)
**Context.** QA review (M2) showed `paper_only` could be renamed away and guarded tables truncated.
**Decision.** Migration `0002_hardening` blocks renaming the `paper_only` row and `TRUNCATE` on `system_config` / `audit_log`. The application connects as the table owner, so a privileged SQL user can still `ALTER`/`DROP` the guards. Nothing in the code reads `system_config.paper_only`: the real enforcement is in code (ADR-001/002). **Before any shared deployment** the API and worker must use a separate, non-owner role (DML only). This is tracked in STATUS.md.

## ADR-011 — Sanity bounds on market data (QA 2026-11-03)
Discovery fails closed with `OUT_OF_RANGE` when:
- mark or oracle price is outside [1e-8, 1e9];
- |funding| > 5% per hour;
- open interest is outside [0, 1e15];
- mark/oracle divergence exceeds 10,000 bps.

Divergence above the configured limit (default 50 bps) but below that hard cap only marks the market `DEGRADED`. The bounds guarantee storable values (NUMERIC(38,18)) and prevent `decimal` arithmetic errors. Integer and boolean metadata fields are validated strictly (no `"20"` → 20 or `true` → 1 coercion). A duplicate dex name gives `DEX_AMBIGUOUS`.

## ADR-012 — Discovery state is ordered by event id, per market (QA 2026-11-03)
`system_events.subject` stores the market. The API compares the latest `MARKET_DISCOVERY_OK` and `MARKET_DISCOVERY_FAILED` events **by primary key**, not by timestamps from two different clocks (worker vs DB `now()`, 1-second SQLite precision). It returns `state = CURRENT | STALE | FAILED`. The UI labels non-current values as "last known" and never shows a green "current" badge after a failure.

Any unexpected exception during discovery, or the DB rejecting the data (`DB_REJECTED`), is recorded as a failure. A DB *outage* is logged and reported as `persisted=false` (worker exit code 3).

## ADR-013 — Destructive DB tests require an explicit `*_test` database (QA 2026-11-03)
`tests/integration/test_postgres.py` drops all tables. It only runs with `AUTOTRADER_TEST_DATABASE_URL` pointing to a database whose name ends in `_test`. Non-live tests are also blocked from opening any non-localhost socket (`tests/conftest.py`).

---
# Phase 1 — Market Data Engine

## ADR-014 — REST is the source of truth for closed bars; WS only updates forming bars (2026-11-03)
**Context.** The WS stream can drop, duplicate or reorder frames, and doesn't say "this bar is final".
**Decision.**
- Only REST (`candleSnapshot`) marks a bar `is_closed`, and only once `now ≥ close_time + 5 s` on our clock.
- WS updates touch forming bars only. An update with lower volume or trade count than what is stored is out-of-order and ignored.
- The collector re-syncs from REST after every (re)connect and every 30 s (finalize), starting at the last closed bar. This closes bars and fills anything the stream missed.
**Consequences.** Closed history never depends on WS delivery. Strategy inputs (closed bars) can lag the true close by up to ~35 s.

## ADR-015 — Closed bars are immutable; problems are recorded, never fixed (2026-11-03)
- One row per (coin, interval, open_time), enforced by a unique key. All writes are idempotent upserts.
- A later REST value that differs from a stored closed bar is **not** applied. It is recorded as `REVISED_CLOSED_CANDLE`, which keeps backtests reproducible.
- Invalid bars are rejected and recorded as `REJECTED_CANDLE` with the raw payload: non-positive or out-of-range prices, OHLC inconsistency, negative volume, misaligned time, wrong market, floats, NaN.
- Conflicting duplicates are recorded as `DUPLICATE_CONFLICT`.
- Missing bars are recorded as `GAP` (with the count) and **never synthesized**. A gap is marked resolved only if real data later arrives; the row is kept for audit.
- Outliers are **flagged, not removed**: a close-to-close move above 8% (30m) or 10% (1h), or a high–low range above 10% / 12%. These thresholds sit above the largest moves in the available history (3.7% / 6.0%).

## ADR-016 — No look-ahead by construction (2026-11-03)
Strategy and backtest code must read bars only through `closed_candles(coin, interval, as_of=T)`. It returns only bars with `is_closed AND close_time ≤ T`, so a bar ending after T is invisible. The API applies the same rule with its own clock, and returns the forming bar separately as `forming` (display only).

## ADR-017 — History depth is what the API provides (2026-11-03)
`candleSnapshot` is called with `startTime=0` and the real range is recorded per run (`ingestion_runs`). Observed on 2026-11-03:
- 30m: 5,000–5,008 bars from 2026-07-22 (~104 days);
- 1h: 5,000–5,004 bars from 2026-04-09 (~208 days).

The database keeps all bars it has ever collected, so coverage grows over time while the collector runs. Longer history requires a separately licensed and verified source (blueprint §3).

## ADR-018 — WebSocket guard and single writer (2026-11-03)
- `common/ws.py` is the only WebSocket client, mirroring the HTTP allow-list. It connects only to the exact `wss://api.hyperliquid.xyz/ws`.
- Outgoing frames are limited to `subscribe` / `unsubscribe` / `ping` for the public channels `candle`, `activeAssetCtx`, `l2Book` and `trades`.
- Hyperliquid's WS `post` method (which can carry signed actions) and every user/account channel are refused before sending.
- A `SafetyViolationError` stops the collector instead of triggering a reconnect.
- A PostgreSQL advisory lock allows exactly one collector to write. A second instance exits with code 5 (verified live).

## ADR-019 — Least-privilege database users (2026-11-03; resolves ADR-010 follow-up)
- Migration 0003 creates the group roles `autotrader_reader` (SELECT) and `autotrader_writer`.
- `autotrader_writer` gets SELECT everywhere, but INSERT/UPDATE only on the market-data and event tables. It has no DELETE, TRUNCATE or DDL, and no write access to `system_config` or `audit_log`.
- The login users `autotrader_api` and `autotrader_worker` come from `infra/db/roles.sql` (run automatically by the compose DB on first start).
- Only migrations use the owner account.
- A test asserts every table is granted to both roles, so future tables can't be forgotten.

## ADR-020 — Closing is decided at request time; shutdown drains writers (2026-11-04, final review)
- A REST snapshot can close a bar only if the bar had ended (plus 5 s) **when the request was sent**, never by the response time. A slow response must not freeze partial data (H1).
- Candle writes hold `_write_lock` only around DB transactions; whole REST syncs are serialised by `_sync_lock`, and network I/O never holds the write lock (M2).
- On shutdown the collector drains in-flight syncs and refuses new writes **before** reporting STOPPED and before the single-writer lock is released (M4). `autotrader backfill` takes the same lock.
- Closed candles are immutable in the database itself (migration 0004 trigger).

## ADR-021 — Controlled, audited correction of closed candles (2026-11-04)
- Closed candles stay immutable by default (migration 0004).
- Migration 0005 adds a single correction path, usable only by a non-service DB user with a transaction-local `autotrader.correction_ticket`. It may not change a bar's identity or reopen it, and the trigger writes before/after into the append-only `audit_log`.
- Corrections are made only with `autotrader repair-candles --apply --ticket … --backup-dir …`: a JSON backup is written before any change, everything happens in one transaction under the writer lock, and `REVISED_CLOSED_CANDLE` is recorded.
- Nothing ever corrects data automatically.

## ADR-022 — Phase 2 research protocol fixed before results (2026-11-04)
- **Primary result** = each strategy's pre-registered default parameters (blueprint / strategy definition, not fitted to this data) on all available history.
- Chronological **70/15/15** split (development / validation / holdout). Each segment uses earlier bars only for indicator warm-up and is force-closed at its end.
- **Optimisation** only on development (small grid, objective = after-cost expectancy in R with ≥ 8 trades). The chosen parameters are hashed and evaluated once on validation and holdout. Holdout is never used for selection (tested).
- **Walk-forward**: rolling 42-day train → 14-day test; only test trades are aggregated.
- **Verdict**: `PASS` only if all criteria hold (0 audit violations, ≥ 30 trades, 95% bootstrap CI of expectancy > 0, holdout ≥ 10 trades and net > 0, walk-forward OOS > 0, still profitable at 2× costs, ≥ 180 days of 30m history). `FAIL` only if ≥ 30 trades and the CI is entirely below 0. Otherwise `INCONCLUSIVE` with the failed criteria. With ~104 days of 30m history a PASS is impossible by construction (ADR-006).

## ADR-023 — Execution model for OHLC-only data (2026-11-04)
- Decisions are made at the close of a closed 30m bar; entries fill at the **next bar's open** (never the signal close). The last bar of a window is reserved for the forced exit.
- Market fills (entries, stops, time exits) pay half-spread + adverse slippage; prices are rounded to the market tick in the adverse direction (5 significant figures, ≤ 3 decimals for `xyz:CL`).
- Stop: stop-market at the stop level, or at the open if the bar gaps through it (loss may exceed the plan; reported, not hidden).
- Target: limit; counts only if the bar trades **through** it (touch is not enough); a gap beyond it fills at the target, not better; taker fee is still charged.
- **Stop and target in the same bar → stop first** (default). `TARGET_FIRST` and `OPEN_PROXIMITY` exist only as sensitivity scenarios.
- No bar after the signal, a data gap, or a zero-volume entry bar → no fill (`NO_NEXT_BAR`, `DATA_GAP`, `NO_PLAUSIBLE_EXECUTION`).

## ADR-024 — Costs: verified vs assumed (2026-11-04)
- **Verified:** hourly funding from Hyperliquid `fundingHistory` (stored in `funding_rates`, 7,242 hours since the market opened on 2026-01-06, no missing hour); candles; tick/lot precision. Positive rate ⇒ longs pay. Funding is charged for hours `entry < h ≤ exit` on notional at the 30m close at that hour (oracle history is not available — approximation).
- **Assumed (labelled in every report):** taker fee 0.10% per fill (blueprint baseline; actual HIP-3 fee not verifiable here), half-spread 0.01%, slippage 0.05% per side, 2× spread/slippage outside the CME session, missing funding hour = 0.00125%/h against the position, minimum order 10 USDC.
- Stress scenarios: costs ×0.5 / ×2, slippage ×2 / ×10, funding zero / ×2 / always adverse, skipped fills, one-bar delay, no session gate, alternative ambiguity rules, ±20% parameter perturbation.

## ADR-025 — WTI reference session gates entries (2026-11-04)
- `xyz:CL` trades 24/7 but its reference (NYMEX CL on CME Globex) does not: recorded weekend volume is ~10× lower. New entries are allowed only Sun 18:00 → Fri 17:00 New York time, outside the 17:00–18:00 daily break, not within 30 min of an open/close, not on CME holidays, and not on the 2 business days up to each CL expiry. Outside 2025-01-01…2027-12-31 the calendar is unknown and entries are blocked (fail closed).
- The schedule and holidays are from prior knowledge (CME pages unreachable here) — **UNVERIFIED**. Exits stay possible off-hours (the perp trades) with doubled spread/slippage.

## ADR-026 — Risk rules in the backtest (2026-11-04)
- 1,000 USDC virtual equity; risk 0.25% per trade sized to the stop **including** fees, spread and worst-case slippage; notional ≤ 2× equity; one position; ≤ 4 entries per UTC day; daily loss 1% (realised + conservative unrealised) blocks entries until 00:00 UTC; 5% drawdown from the high-water mark halts entries for the rest of the run.
- Deviation from the Phase 3 rule: 3 consecutive losses pause entries until 00:00 UTC (a backtest can't wait for a manual review).

## ADR-027 — Runs are immutable and audited (2026-11-04)
- Every run is checked by an independent auditor (`backtest/validation.py`): no look-ahead, reference prices inside the traded bar, fills never better than the reference, exact accounting identity, risk limits, no overlap. Any violation ⇒ `INVALID`.
- Runs are stored in `backtest_runs` / `backtest_trades` with parameter hash, data fingerprint, git commit and report sha256; a trigger rejects UPDATE/DELETE. The API only reads them (GET); runs are created by `autotrader backtest`.

## ADR-028 — Taker fee measured from the exchange's own records (2026-11-04, research audit)
**Context.** The official fee page (`hyperliquid.gitbook.io/.../trading/fees`) is not reachable from the research environment (network policy). The 0.10% fee was the blueprint's placeholder.
**Evidence (public, unsigned Info API; no keys):**
- `userFees` schedule: perp tier 0 = 0.045% taker / 0.015% maker; VIP tiers down to 0.024% / 0; referral discount 4%; staking discounts 5–40%.
- `metaAndAssetCtxs` for `xyz:CL`: `deployerFeeScale = 1.0`, `growthMode = enabled`, `lastFeeScaleChangeTime = 2026-01-06` (market launch).
- 21,260 `xyz:CL` fills of 28 addresses taken from the public trade stream (`userFills`), each compared with that user's `userFees` tier: the charged fee is **0.2 × the user's tier rate**. Tier-0 takers paid **0.0090%** per fill (0.00864% with the referral discount), every month Sep–Nov 2026 (437 fills). Jul–Aug was not observed.
- 0.2× fits "HIP-3 fee 2× base, growth mode −90%", but that explanation is **inferred**, not read from documentation.
**Decision.** Baseline taker fee = 0.0090% per fill, provenance `OBSERVED`. It is still charged on targets too (no maker rebate assumed). Scenarios 0.0045%, 0.045%, 0.09% (growth mode off, inferred) and 0.10% (old assumption) are in every report. Slippage stays at the conservative 0.05% per side and is now the largest cost.

## ADR-029 — The holdout of the current dataset is exposed (2026-11-04, research audit)
- In the first PR #3 runs, rolling walk-forward folds covered the whole history, so the last folds **selected parameters on data inside the 70/15/15 holdout** (2026-10-19 → 2026-11-04). Holdout results were also reported (runs 1–6).
- Fix: walk-forward now ends at the holdout start (`until`); every train window, test window and parameter selection lies in development + validation. Tested by spying on every selection run.
- The dev-only optimisation never touched the holdout (re-verified).
- Even so, the holdout of this dataset has been **seen**. `Protocol.exposed_until = 2026-11-04T08:00Z` records it. The report field `independent_test` is `NONE`, and the new verdict criterion `holdout_untouched` makes PASS impossible until a holdout starts after that instant. **There is currently no untouched independent test**; only forward data after 2026-11-04 08:00 UTC can provide one.
- Re-runs for comparison use `autotrader backtest --as-of 2026-11-04T08:00:05Z` (same data fingerprint `f869a045…`), so the never-seen data stays unused.

## ADR-030 — xyz:CL trades 24/7; the CME calendar is only a strategy filter (2026-11-04, research audit)
- Observed: every one of the 5,000 30m bars (and 5,000 1h bars) has trades, with at least 31 trades per 30m bar, including all weekends and CME holidays. There are no gaps.
- While CME is closed, median 30m volume is ≈5.6× lower (6,994 vs 39,253) and there are ≈2.3× fewer trades. That is the basis for the entry filter and for the 2× off-hours spread/slippage assumption.
- The engine never blocks exits on CME hours. Entry blocks are categorised (`STRATEGY_FILTER`, `RISK`, `DATA`, `MARKET`, `SIMULATION`); CME-based codes are `STRATEGY_FILTER`, not "market closed". Reports include a `market_hours` section computed from the data.

## ADR-031 — Forward tests are frozen, journaled and recomputed (2026-11-04, Phase 2.5)
- `autotrader forward freeze` creates one **immutable** `forward_tests` row per strategy. It stores:
  - parameters (with hash), cost model, risk limits and execution settings;
  - verdict thresholds;
  - the git commit and `code_hash`, a sha256 of the strategy and engine source files;
  - the latest backtest with the **same parameter hash**, as the reference expectation.
- The test starts at the next 30m boundary after freezing, and is always later than the exposed data (ADR-029).
- One active test per strategy. A stopped test is never resumed; a new freeze is a new test.
- The forward worker appends one `forward_decisions` row per closed 30m bar after the start, holding:
  - the decision and its reason, and the indicator rationale;
  - a market snapshot: OHLCV, last funding, and mark/oracle when a fresh discovery exists;
  - the strategy version, the parameter and code hashes, and an input digest;
  - `evaluated_at` and a `late` flag for catch-up after an outage.
- Database triggers reject UPDATE/DELETE on all forward tables, and reject decisions for bars before the start. A CHECK enforces `evaluated_at >= bar_close_time`.
- Indicators always start at the frozen `history_anchor` (the first stored 30m bar), so every decision can be reproduced bit-for-bit later. Bars before the anchor are not used, so the 1h trend EMAs differ negligibly from a backtest that has 208 days of 1h history.
- If `code_hash` changes, the worker writes `INTEGRITY_HALT` and records nothing more. Strategies can't be "improved" mid-test.
- **Results are never stored.** Every report is recomputed from the frozen config, the journal, the immutable candles and the append-only funding. It uses the same `Engine` as the backtest, in `live_edge` mode: an open position stays open, and a signal on the last bar waits for the next bar.
- Bars that closed but were not yet journaled are not simulated.
- Comparison with the backtest:
  - **Parity:** each journal decision is recomputed with the backtest code path, and any mismatch makes the result INVALID.
  - **Consistency:** expectancy, win rate, profit factor, trades and signals per day, and drawdown vs the reference run. "Consistent" means the backtest expectancy lies inside the forward 95% CI, judged only with ≥ 10 forward trades.
- Verdict:
  - INCONCLUSIVE until ≥ 30 closed trades, ≥ 30 days and ≥ 15 distinct trade days.
  - Then PASS only if the CI is above 0 and the strategy is still profitable at 2× costs; FAIL if the CI is below 0.
  - INVALID on a code change, a parity mismatch or an audit violation.
