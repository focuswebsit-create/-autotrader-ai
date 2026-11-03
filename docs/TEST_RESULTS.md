# Test Results — Phase 0 (2026-11-03)

Environment: Linux cloud dev container, Python 3.12.3 (also 3.13), PostgreSQL 16 (local), Node 22, Next.js 16.4.4.

## Backend (CI-equivalent sequence)

```
uv sync --frozen --python 3.12
uv run ruff check src tests            -> All checks passed!
uv run ruff format --check src tests   -> 38 files already formatted
uv run mypy                            -> Success: no issues found in 29 source files (strict)
uv run autotrader safety-check         -> exit 0
uv run alembic upgrade head && alembic downgrade base && alembic upgrade head -> OK
uv run pytest --cov=autotrader         -> 93 passed, 3 skipped (live, opt-in); coverage 95%
```

## Live, read-only Hyperliquid mainnet (opt-in: `RUN_LIVE_TESTS=1 uv run pytest -m live -s`)

```
PASSED test_live_discovery_of_xyz_cl
  "dex_index": 1, "coin": "xyz:CL", "asset_index_in_dex": 29,
  "sz_decimals": 3, "max_leverage": 20, "mark_px": "95.53", "oracle_px": "95.53",
  "funding_rate": "0.00000625", "status": "ACTIVE"
PASSED test_live_candle_availability[30m]  -> 30m: 5005 candles, 104.2 days
PASSED test_live_candle_availability[1h]   -> 1h: 5003 candles, 208.4 days
```

Full suite with live + DB enabled: **96 passed**.

## Safety tests (what they prove)
- `/exchange` (any case, path tricks, query, testnet) is blocked **before** the request reaches the network; the recording transport sees zero requests.
- Only `https://api.hyperliquid.xyz/info` + `POST` + allow-listed `type` is accepted; other hosts, ports, `http`, userinfo tricks and signing fields are rejected.
- Startup aborts with wallet/key-like env vars or `TRADING_MODE != PAPER` (API, worker, CLI).
- No signing libraries in dependencies, lockfile or imports; no `/exchange` literal outside the deny-list.
- All API routes are `GET`; a `POST` to the API returns 405.
- DB: `paper_only` cannot be set false or deleted; `audit_log` UPDATE/DELETE rejected.

## End-to-end run
```
alembic upgrade head
python -m autotrader.worker   -> "market discovered" coin=xyz:CL status=ACTIVE, exit 0
uvicorn autotrader.api.main:app
GET  /health                  -> {"trading_mode":"PAPER","paper_only":true,"database":"ok",...}
GET  /api/market/discovery    -> live xyz:CL snapshot from DB
POST /api/market/discovery    -> 405
next start (apps/web)         -> page renders backend state; Hebrew error shown when API is down
```
Screenshot (iPhone 14 viewport): `docs/screenshots/phase0-iphone.png`.

## Frontend
```
npm run lint       -> no problems
npm run typecheck  -> no errors
npm run build      -> Compiled successfully; "/" is dynamic (server-rendered on demand)
```

## Defects found and fixed during Phase 0
1. Static safety scan flagged an `/exchange` mention in a docstring → reworded (the check worked as intended).
2. Coin `xyz:` (empty symbol) was accepted → validation now requires a non-empty symbol.
3. Live calls failed behind an HTTPS proxy because a custom httpx transport ignores proxy env vars → ADR-009.
4. RTL rendering scrambled parentheses in the market card title → Latin parts are wrapped in `<bdi>`.
