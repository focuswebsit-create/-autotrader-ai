# AutoTrader AI — PAPER ONLY

Research and **paper-trading** (simulation) system for Hyperliquid's HIP-3 WTI oil perpetual **`xyz:CL`**.

> ⚠️ **PAPER / NO REAL MONEY.** This system reads public mainnet market data only. It has no wallet, accepts no private keys, and can't send orders: the authenticated `/exchange` endpoint is blocked in code and by tests.

Specification: [`PROJECT_BLUEPRINT.md`](PROJECT_BLUEPRINT.md) · Working rules: [`CLAUDE_MASTER_PROMPT.md`](CLAUDE_MASTER_PROMPT.md)
Phase 2 results: [`docs/PHASE2_REPORT.md`] (Backtest Lab at http://localhost:3000/backtest) · Phase 2.5: [`docs/PHASE2_5_REPORT.md`] (http://localhost:3000/forward) · Deployment plan: [`docs/DEPLOYMENT.md`] · Status: [`docs/STATUS.md`](docs/STATUS.md) · Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md) · Evidence: [`docs/TEST_RESULTS.md`](docs/TEST_RESULTS.md)

## Layout

```
src/autotrader/
  common/     safety (PAPER-only guards), read-only HTTP client, config, logging
  market/     runtime discovery of xyz:CL (perpDexs + metaAndAssetCtxs, dex=xyz)
  db/         SQLAlchemy models, Alembic migrations, repository
  api/        FastAPI (GET-only)
  ingest/     REST backfill/sync + live WS collector (Phase 1)
  worker/     discovery job
  reporting/  data coverage report
  strategy/   indicators, CME session calendar, 3 deterministic strategies (Phase 2)
  backtest/   engine, costs, audit, metrics, research protocol, storage (Phase 2)
  forward/    frozen forward tests: recorder, evaluator, worker (Phase 2.5)
  risk/ paper/   (Phase 3)
apps/web/     Next.js Hebrew RTL dashboard
infra/docker/ Dockerfiles
infra/db/     least-privilege DB users (roles.sql)
tests/        unit, integration (PostgreSQL, opt-in live), recorded fixtures
docs/         STATUS, DECISIONS, TEST_RESULTS, screenshots
```

## Run locally

**With Docker:**

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

The web app runs at http://localhost:3000 and the API at http://localhost:8000/health.

**Without Docker** (requires `uv`, Node 22, PostgreSQL 16):

```bash
uv sync
```

```bash
export AUTOTRADER_DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/autotrader
```

```bash
uv run alembic upgrade head
```

```bash
uv run autotrader safety-check
```

```bash
uv run autotrader backfill        # fetch all available history (REST)
```

```bash
uv run autotrader collect         # live collector: discovery, backfill, WS stream, gap fill
```

```bash
uv run autotrader data-report     # coverage, gaps and data-quality issues (JSON)
```

```bash
uv run autotrader funding-sync    # recorded hourly funding (fundingHistory, read-only)
```

```bash
uv run autotrader backtest        # Phase 2 research protocol for all strategies -> backtest_runs
```

```bash
uv run autotrader forward freeze  # Phase 2.5: freeze strategies, start forward tests (once)
```

```bash
uv run autotrader forward run     # Phase 2.5: record decisions as 30m bars close (keep running)
```

```bash
uv run autotrader forward status  # forward results, recomputed from the journal (JSON)
```

```bash
uv run uvicorn autotrader.api.main:app --port 8000
```

```bash
cd apps/web && npm ci && API_BASE_URL=http://localhost:8000 npm run dev
```

## Checks

```bash
uv run ruff check src tests && uv run mypy && uv run pytest
```

PostgreSQL tests drop and recreate tables, so they only run against a dedicated database whose name ends in `_test`:

```bash
AUTOTRADER_TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/autotrader_test uv run pytest -m db
```

```bash
RUN_LIVE_TESTS=1 uv run pytest -m live -s
```

```bash
cd apps/web && npm run lint && npm run typecheck && npm run build
```
