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
