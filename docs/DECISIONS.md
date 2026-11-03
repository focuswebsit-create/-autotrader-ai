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
