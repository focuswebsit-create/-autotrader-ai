# AutoTrader AI — Hyperliquid `xyz:CL` Paper-Only Blueprint

**Version:** 0.1 | **Date:** 2026-11-03 | **Phase:** research → backtest → forward paper trading

## 0. Non-negotiable scope

Build a production-quality **research and paper-trading system**, **not** a live trading robot. Read **public mainnet data only** from Hyperliquid's `xyz:CL` HIP-3 WTI oil perpetual market. Never ask for, store, generate, or use wallet private keys or API agents in v0.1. Never send orders or other signed actions to `/exchange`. There must be no working live order execution path, even behind a feature flag. UI must visibly say **PAPER / NO REAL MONEY**. Paper account begins with a configurable 1,000 USDC virtual balance.

Success is **reproducible and honest evaluation**, not claimed profitability. An AI assistant may explain strategy decisions, but may not generate an executable trade instruction or override the deterministic risk engine.

## 1. Core architecture

```
Hyperliquid MAINNET Info API + WS (read only)
                   |
          Market Metadata Service
    (resolve xyz:CL, dex, tick/size precision,
     status, mark/oracle, funding, leverage limit)
                   |
          Collector & Data Validation
    (REST backfill, WS updates, UTC timestamps,
     closed-bar normalization, gap/duplicate checks)
                   |
            PostgreSQL / Supabase
   (candles, orderbook snapshots, funding, signals,
    paper fills, paper positions, audit, runs)
                   |
          Deterministic Strategy Engine
     (same function for backtest + forward paper)
                   |
              Risk Engine
    (fail-closed validations + daily/monthly guardrails)
                   |
             Paper Broker
  (bid/ask/depth/slippage model, virtual funding/fees)
                   |
      Dashboard + alerts + exportable reports
```

**Suggested stack:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, NumPy/Pandas for research, pytest/hypothesis; PostgreSQL 16+ (Supabase compatible); TypeScript/Next.js for responsive RTL Hebrew dashboard, Tailwind/shadcn, lightweight TradingView chart library; Docker Compose for local development; persistent background-worker hosting (not Vercel serverless for the stream); GitHub Actions CI. Redis is optional and should not be required for MVP. Frontend may be on Vercel; collector/strategy worker needs an always-running process. Decimal for monetary/order quantities, UTC throughout, America/New_York only when evaluating external futures market session with DST and official holiday calendar.

**Separate packages/services:** `apps/web`, `services/api`, `services/worker`, `src/market`, `src/strategy`, `src/risk`, `src/paper`, `src/backtest`, `src/reporting`, `src/common`, `infra`, `tests`, `docs`.

## 2. Exchange and symbol discovery

1. Use `POST https://api.hyperliquid.xyz/info` (public, unsigned) to obtain `perpDexs` and `metaAndAssetCtxs` with `dex: "xyz"`.
2. Resolve and validate the exact market name `xyz:CL` at runtime. Never assume native `CL` or hard-code an asset ID. For any eventual authenticated trading in a different future project, HIP-3 asset IDs have a special dex-index formula; this v0.1 does not execute orders.
3. Fetch mark and oracle price, current funding, available market metadata, book depth and status. Check whether this market exists and is active on mainnet; **do not assume `xyz:CL` exists on testnet**.
4. WS: `wss://api.hyperliquid.xyz/ws`, subscriptions to `candle` for `xyz:CL` at 30m and 1h, `l2Book`, trades as useful. Add ping, exponential reconnect, stream sequence/time sanity checks, REST backfill after disconnect, and alert if stale.
5. Source-specific hazards: external WTI futures reference may close while HIP-3 perp continues trading; avoid new entries during external-reference off-hours and around session/rollover until verified calendars and precise status are available. Fail closed if unavailable. Track oracle/mark divergence, market halts, contract-reference rolling and abnormal funding.

## 3. Historical data acquisition and honesty

Hyperliquid `candleSnapshot` is limited to **the most recent 5,000 candles for each interval** (not 5,000 for every call). At 30m this is about 104 days of data; at 1h about 208 days. Fetch what is actually available, record the exact range and gaps, and never claim a 12-month backtest unless separately sourced legitimate historical market data has been acquired and verified. The public archive is not a ready-made OHLCV candle archive; it offers different data such as L2 snapshots and node fills, with possible cost/missing periods. Alternate vendors must be explicitly disclosed and checked for licensing, market identity, timestamp alignment, completeness, and survivorship.

Data tables should include `symbol`, `timeframe`, `open_time`, `close_time`, OHLCV, `is_closed`, `source`, `received_at`, integrity hash / batch id, and unique `(symbol,timeframe,open_time)`. Never use unfinished candles for signal generation. Check monotonicity, positive values, high>=max(open,close), low<=min(open,close), nonnegative volume, duplicate candles, gaps, and outlier flags. Store unavailable/missing observations as missing, **never fill synthetic prices silently**.

## 4. First hypothesis to test — Trend-Filtered 30m Breakout

Use a single simple deterministic strategy, deliberately **not assumed profitable**:

- **Primary market:** `xyz:CL`; signal candles: 30m **fully closed**; trend confirmation candles: 1h **fully closed**.
- **Long condition:** 1h EMA(20) > EMA(50); the latest completed 30m candle closes above the maximum *high of the previous 20 completed 30m bars*, excluding the current bar; 30m RSI(14) between 52 and 70; volume on the signal bar >= 1.2 times median volume of the previous 20 completed 30m bars.
- **Short condition:** 1h EMA(20) < EMA(50); latest completed 30m candle closes below the minimum *low of the previous 20 completed 30m bars*; RSI(14) between 30 and 48; signal bar volume >= 1.2 times the 20-bar median.
- **Entry:** earliest eligible next-bar opportunity, priced with spread and adverse slippage, and only after every eligibility gate passes. Never enter at the signal candle's close in a backtest if the close wasn't tradable at that moment.
- **Stop:** 1.5 x ATR(14) on the completed 30m signal candle from actual simulated entry fill, in unfavorable direction.
- **Take profit:** 2.5R from actual simulated fill, where R = absolute entry minus stop. Fixed bracket for v0.1; no trailing/averaging into losers.
- **Position:** only one open position, no overlapping positions. Record full rationale, input candle IDs, expected risk and fees. Skip if data insufficient for indicator warm-up, risk gates fail, no plausible execution, reference session is closed, or funding/mark oracle is anomalous.
- **Conflict:** when both TP and SL are touched by the same historical OHLC bar and no intrabar evidence exists, assume **SL first**. Run sensitivity tests with different assumptions.
- **Parameterization:** expose EMA windows, breakout lookback, ATR multiple, RSI limits and volume threshold for research; lock parameters before holdout evaluation. Never optimize against final test set.

These are research assumptions, not buy/sell advice.

## 5. Paper broker & transaction cost modeling

Initial cash 1,000 virtual USDC, configurable. Track equity, balance, realized/unrealized P&L, funding, fees, spread, slippage, margin, effective leverage, and bankruptcy/negative-equity semantics. Use the *same* paper fills/risk accounting in historical replay and forward paper trading (with distinct data adapters).

For live paper execution, model buy at ask / sell at bid with a depth-aware conservative slippage estimate. For OHLC-only historical tests, include configurable estimated half-spread and adverse slippage. Baseline conservative assumptions until verified: **0.10% taker fee per fill plus 0.05% adverse slippage per side**, with explicit separate spread if applicable. These numbers are **model assumptions**, not asserted actual exchange fees. Run stress tests at 0.5x, 1x, and 2x baseline transaction costs. Funding is hourly for perpetuals: use recorded actual funding if available, otherwise disclose missing data and provide a conservative sensitivity model. Never present gross P&L as net P&L. Calculate all quantities with exchange-validated precision and market-specific min size.

## 6. Hard risk controls — fail closed

Default controls, editable only in a restricted config with audit history:

- `paper_only=true`, `initial_equity_usdc=1000`, `max_risk_per_trade_pct=0.25`, `daily_loss_limit_pct=1.0`, `high_water_drawdown_halt_pct=5.0`.
- `max_effective_leverage=2.0`, `max_open_positions=1`, `max_entries_per_day=4`, `halt_after_consecutive_losses=3`, no martingale/grid/averaging down.
- Compute risk-based size as `risk_budget_usdc / abs(entry - stop)`, then cap by market precision, min notional/size, available paper collateral, and max notional (2x equity). Recompute after fees and slippage and reject if actual potential loss exceeds budget.
- Max spread default 15 bps; configurable and validated against observed symbol liquidity. Reject no-book, crossed book, stale quotes, price jumps and abnormal oracle-mark divergence. These are conservative *initial* settings, not guaranteed optimal values.
- Stop opening new positions when market data is stale, WS disconnected, API rate limit repeatedly hits, metadata missing, DB unavailable, abnormal volatility, external reference session closed, market is halted, or any risk state is uncertain.
- Panic / kill switch disables new entries immediately; open paper positions can be force-flattened in simulator with realistic exit costs. A network outage must never be interpreted as proof that a real position is closed.
- Persist risk state and open positions across restarts; implement locking, idempotency, ordered events and replay-safe fills. No duplicate entries on reconnect or repeated candle close.
- Daily accounting resets at 00:00 UTC explicitly; loss limits count realized + unrealized P&L with conservative marking. Maximum drawdown is relative to high-water equity; manual review required to resume from drawdown halt.
- UI must show reason for each BLOCKED signal and HALTED state. No 'turn off risk checks' convenience control.

## 7. Backtest validity

1. Reproduce indicators from finalized, time-aligned candles; prohibit look-ahead, current-bar peeking, label leakage, and impossible fill prices.
2. Split chronologically (example 70% development, 15% validation, 15% untouched holdout) and implement expanding/rolling walk-forward. Report exact dates and number of trades per segment.
3. Report trade-level audit: signal, trigger, execution approximation, stop, TP, fee, funding, slippage, risk gate result, reason for exit, and cash ledger.
4. KPI dashboard: net return, max drawdown, profit factor, win rate, average win/loss, expectancy per trade, number of trades, time in market, longest losing streak, fee drag, exposure, and uncertainty / sample adequacy. Sharpe/Sortino when return series is adequate.
5. Stress: 2x fees, 2x slippage, 10x slippage shock, worse fills, intrabar SL-first, skipped fills, gaps, outlier candles, weekend/holiday transitions, mark/oracle divergence, rate-limit and data outage.
6. Require meaningful out-of-sample data and broad regime coverage before calling results informative. If history or trade sample is small, label **INCONCLUSIVE**, not PASS. Zero look-ahead violations and zero risk-engine violations are mandatory.

## 8. Forward paper-trading acceptance

- At least 30–60 calendar days of continuous paper observation *after* backtest, but extend as needed for useful statistical sample and multiple regimes.
- Logs must confirm 0 unauthorized live requests, 0 duplicate orders, 0 incorrect accounting entries, 0 risk-limit violations. System must recover from deliberate process kills and WS disconnections without double trades.
- Every trade is reconstructable from candle IDs and deterministic strategy version/commit hash.
- Paper performance must incorporate all modeled fees, market-impact assumptions, and funding. Compare projected and observed paper slippage.
- Even after passing paper tests, **do not automatically enable live trading**; a new separate architecture/security/regulatory review and explicit human decision would be required.

## 9. Database schema (minimum)

`market_metadata`, `candles`, `book_snapshots`, `funding_rates`, `market_sessions`, `strategy_versions`, `backtest_runs`, `backtest_trades`, `signals`, `risk_decisions`, `paper_accounts`, `paper_orders`, `paper_fills`, `paper_positions`, `equity_snapshots`, `system_events`, `alerts`, `audit_log`, `system_config`. Include unique keys and foreign keys, immutable historical run configs, and timestamps in UTC. Use RLS/auth for the web dashboard if exposed online.

## 10. UX requirements (Hebrew RTL mobile first)

Overview (equity, daily P&L, drawdown, halted/active), CL live candlestick chart with indicators and strategy overlays, Signals with **why** a signal was accepted or blocked, Virtual Positions with Stop/TP, Backtest Lab with date range, cost assumptions and strategy version, Trade Journal with CSV export, Risk Center (read-only limits and kill switch), Data Health/WS status, Settings. Never label a paper price as an executed market order or mark assumptions as facts. Support light and dark, performant mobile views, role-based access.

## 11. Security and operations

- No wallet connection, no private key input, no agent key, no authenticated exchange endpoint, no USDC handling in v0.1.
- Use secrets manager only for optional notifications and database credentials. `.env.example` dummy values only, no secrets in logs or Git.
- API request allow-list for `https://api.hyperliquid.xyz/info` and approved data WS; automated tests fail if code sends `/exchange` traffic.
- Health endpoints, structured logs (request ID, run ID, strategy hash), metrics, alerting for stale WS / missing candles / halted state, migrations, backups, restart/replay tests.
- Branch protections, dependency scans, code review, CI: `ruff`, `mypy` where configured, `pytest`, frontend lint/typecheck/build, integration tests.

## 12. Delivery phases and acceptance gates

### Phase 0 — Discovery & scaffolding
Verify exact `xyz:CL` market from runtime metadata, source availability, external pricing schedule, and documented fee behavior. Initialize monorepo, Docker Compose, DB, CI, README, decision log. **Gate:** startup performs public, read-only discovery and proves it cannot trade.

### Phase 1 — Ingestion and quality
Build 30m/1h backfill (subject to 5k cap), persistent WebSocket worker, gap/duplicate validator and market-health flags. **Gate:** restart and disconnect/reconnect recover without loss or duplicate candles; all observations have provenance.

### Phase 2 — Deterministic backtester
Build strategy, same shared signal engine, accounting, broker fills, chronological split, reporting and fee/funding model. **Gate:** deterministic seeded results, clean no-look-ahead tests, extensive edge-case coverage.

### Phase 3 — Autonomous forward paper engine
Run always-on with virtual account, stop/TP engine, state persistence, idempotency, conservative price and fill model, kill switch. **Gate:** chaos tests of reconnect/crash/DB outage and no duplicate fills.

### Phase 4 — Dashboard + alerts
Hebrew RTL responsive views, trade explanations, backtest controls, exports, admin auth and alerting. **Gate:** works on iPhone, accessible charts, results match backend exactly.

### Phase 5 — Observation & evidence
Paper-forward 30–60+ days, compare costs, drawdown and out-of-sample vs training, compile risk/performance evidence. **Gate:** label results PASS / FAIL / INCONCLUSIVE with explicit trade counts and sample dates. No live launch.

## 13. Essential official docs

- API index / Python SDK: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- Perps metadata: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Asset IDs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids
- Candles: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint
- WebSocket: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- Subscriptions: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- Historical archive caveats: https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
- Trading fees: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- HIP-3: https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals

## 14. Deliverables checklist

- [ ] Buildable repo with Docker Compose and safe env example
- [ ] Clean database migrations and reproducible test data
- [ ] Read-only HIP-3 market discovery
- [ ] Historical candle collector with coverage report
- [ ] Reconnecting live data worker with fail-closed health gates
- [ ] Deterministic indicator and strategy library
- [ ] Full transaction cost model and paper broker
- [ ] Risk engine, kill switch, daily and max drawdown halts
- [ ] Backtest report and holdout/walk-forward stats
- [ ] Forward paper-account order/position journal
- [ ] RTL mobile dashboard with all paper-mode disclaimers
- [ ] Unit, integration, end-to-end, reliability and negative-security tests
- [ ] Deployment / runbook / recovery and operational alerting
- [ ] No live-execution path, no API key or wallet required
