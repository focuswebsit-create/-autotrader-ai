# MASTER PROMPT FOR CLAUDE CODE — AutoTrader AI

You are my principal quantitative-trading systems architect, Python backend engineer, senior Next.js engineer, QA lead and security reviewer. Build a *working*, production-quality research and autonomous **PAPER-only** trading application named AutoTrader AI focused on Hyperliquid's HIP-3 `xyz:CL` oil perpetual market. I am building this using Claude Code and GitHub. Do not just provide a beautiful mockup. Implement the repository incrementally and run real tests.

**FIRST: Read `PROJECT_BLUEPRINT.md` in full and treat it as the product specification.** Check official Hyperliquid documentation at the links in the spec and check the current market metadata instead of relying on assumptions. Explain any blockers or deviations in `docs/DECISIONS.md` and in your final report.

## Non-negotiable safety

1. The only allowed operating mode in this project is PAPER. It may read real mainnet prices (public API and WS), but must NEVER place a real order, withdraw funds, request wallet secrets, create an API agent, or call the authenticated `/exchange` endpoint.
2. Never ask me to paste a wallet private key, seed phrase or live trading API credentials. There is no need for any trading key in this project.
3. Make it impossible to accidentally activate live execution via ENV, UI, hidden route, toggled flag or CLI. Add automated tests asserting that no authenticated exchange endpoint is invoked.
4. All trades, stop losses, take profits, margin, cash and P&L are simulation only. Clearly label UI and reports PAPER, and make any missing assumptions visible.
5. Deterministic decision logic must be separate from any LLM. AI can explain completed signals, never approve a trade or override risk checks.
6. Never deploy to or modify any pre-existing production project or database without a separate explicit instruction. Work in a NEW repo and dedicated development infrastructure.

## Initial technology plan

- Backend/worker: Python 3.12+, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL, pytest; Python SDK or read-only REST/WS clients as appropriate. Use Decimal for pricing/accounting.
- Frontend: Next.js + TypeScript + responsive Tailwind interface with excellent Hebrew RTL mobile UX and chart overlays; dashboard in Hebrew, technology names in English where appropriate.
- Local infra: Docker Compose and `.env.example`. Hosted persistent worker for real-time collection (do not try to make a Vercel request handler run a perpetual streaming service). Frontend and API protected by authentication.
- Observability: structured logs, health endpoints, replayable paper trade journal, alerts and kill switch.

## Build in stages, with acceptance gates

PHASE 0: Create a real repo skeleton, architecture decision record, market discovery client to locate `xyz:CL` via `perpDexs`/`metaAndAssetCtxs` for dex `xyz`, verify read-only network connectivity, bootable frontend/backend, Postgres migrations, CI scripts, and tests. Do not hardcode symbol-to-asset ID. Show exact file tree and command output.

PHASE 1: Implement historical candle retrieval and data-quality reports for 30m and 1h, handling the Hyperliquid 5,000 MOST RECENT candles limit per interval honestly (cannot fabricate long-term history by repagination); persistent WS streaming, reconnect/backfill, duplicate/gap detection, closed-bar-only signals, oracle/mark divergence and stale market indicators. Use `xyz:CL`, not native `CL`.

PHASE 2: Implement deterministic, parametrized 30m breakout with 1h trend filtering as described in the blueprint. Share EXACTLY the same indicator and signal functions between historical backtests and forward paper trading. Use no look-ahead, next-bar execution, realistic spreads, adverse slippage, fees and funding sensitivity. Backtest chronological 70/15/15, walk-forward, stats, and audit trails. Do not assert profits. If data/trade sample is insufficient, output INCONCLUSIVE.

PHASE 3: Build autonomous paper engine with a default 1000 virtual USDC account, 0.25% risk/trade, 1% daily loss stop, 5% equity-high-water drawdown halt, 2x max *effective* leverage, no more than one open position, max four entries/day, halt after three consecutive losses, and strict fail-closed checks. Support virtual bracket orders, realistic fill assumptions, persisted paper account, restarts, duplicate suppression, and immediate kill switch.

PHASE 4: Build premium but legible Hebrew RTL dashboard for mobile and desktop: live CL chart, signals + why, simulated entries/stops/TP, virtual trade journal, equity curve, full backtest report, historical-data quality, risk panel, system health, settings with safe immutable defaults, alerts and CSV exports.

PHASE 5: Add integration, E2E and chaos tests for stale book/WS reconnect, candle gaps, restarts, clock shifts/DST, weekends, market halts, extreme spreads, ambiguous TP/SL on same historical candle, null fee/funding data, negative slippage, size rounding, API 429s, and data provider failures. Document deployment and 30–60 day forward-paper observation protocol. Make no live trading claims.

## How you must work with me

- Begin by reading `PROJECT_BLUEPRINT.md`, summarizing the architecture, listing the exact tasks for Phase 0 and **then implementing Phase 0 immediately**. Do not ask me to choose mundane technical defaults.
- Use clean component boundaries and production-grade type validation, input sanitization, unit tests and database migrations. Every status shown in the UI must derive from actual backend state, never mock static values that look live.
- For each phase deliver: working code, files changed, commands executed, test/typecheck/build results, known limitations, security review, and exact steps for me to run. Stop only at safe, independently useful phase boundaries.
- Before every phase, run existing tests and make a scoped git commit; use branches/PRs for significant changes. Never push to any existing production branch without explicit approval.
- Keep the UI simple for an owner who uses an iPhone. Explain errors in Hebrew and log technical details privately. UI must be fast, accessible, readable, and 100% functional.
- Maintain `docs/STATUS.md`, `docs/DECISIONS.md`, and `docs/TEST_RESULTS.md` with evidence. Expose assumptions transparently.

My primary objective is to see whether this algorithm has an actual risk-adjusted edge. Protect the integrity of the data, tests and risk accounting above all else. Now start with Phase 0.
