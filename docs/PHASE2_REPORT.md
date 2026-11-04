# Phase 2 Report — Backtesting & Strategy Engine (xyz:CL, PAPER-ONLY)

**Date:** 2026-11-04 · **Branch:** `claude/keen-curie-78advu` (from `main` @ `ae4e1cc`, Phase 0 + 1 merged)
**Scope:** deterministic backtester on real Hyperliquid data. No live execution, no wallet, no keys, no orders of any kind.

> **סיכום בעברית:** נבנה מנוע Backtest דטרמיניסטי שמשתמש רק בנרות סגורים אמיתיים של `xyz:CL` ונכנס לעסקה רק בפתיחת הנר הבא.
> כל העלויות נכללות: עמלות, Spread ו־Slippage (הנחות מסומנות) ו־Funding (נתונים אמיתיים מ־Hyperliquid).
> שלוש אסטרטגיות נבדקו על כ־104 ימים של נרות 30 דקות. **אף אחת לא הראתה יתרון אחרי עלויות.**
> אחרי בדיקת האמינות (סעיף 6c, עמלה אמיתית 0.009%): EMA 20/50 + RSI — **FAIL**, Trend Following — **INCONCLUSIVE**, Breakout + Volume — **INCONCLUSIVE** (איזון אפס).
> נתוני ה־Holdout כבר נחשפו — כרגע אין מבחן עצמאי שלא נחשף.
> ההיסטוריה הזמינה (5,000 נרות) קצרה מכדי להוכיח יתרון. לכן אין לעבור למסחר אמיתי, וגם במסחר הווירטואלי של שלב 3 צריך להתייחס לתוצאות כהשערה בלבד.

---

## 1. What was built

| Component | Location | What it does |
|---|---|---|
| Indicators | `strategy/indicators.py` | EMA, Wilder RSI/ATR, prior-window max/min/median in `Decimal`; causal by construction (prefix-invariance tests). |
| Strategy library | `strategy/base.py`, `strategies.py` | `ema_rsi`, `trend_pullback` (trend following), `breakout_volume` (blueprint hypothesis). Pure functions of closed bars, shared with the Phase 3 paper engine. Indicators reset after data gaps. 1h trend uses only 1h bars closed at decision time. |
| Reference session | `strategy/sessions.py` | CME Globex CL hours in America/New_York (DST), daily break, holidays 2025–2027, CL roll window; fail-closed outside the calendar. |
| Engine | `backtest/engine.py` | Event-driven, one position, LONG/SHORT, next-bar-open fills, stop/target brackets, gap fills, ambiguous bars, time exit, hourly funding, risk limits, signal audit trail. |
| Costs | `backtest/costs.py` | Fees, half-spread, slippage, off-hours multiplier, funding modes; tick/lot rounding; provenance table (VERIFIED / ASSUMPTION / UNVERIFIED). |
| Auditor | `backtest/validation.py` | Independent check of every run (look-ahead, impossible prices, accounting identity, risk, overlap). Any violation ⇒ INVALID. |
| Metrics | `backtest/metrics.py` | Win rate, profit factor, net P&L, max drawdown (conservative intrabar mark), expectancy in USDC and R, bootstrap 95% CI, t-stat, streaks, exposure, cost drag, Sharpe/Sortino (≥ 30 days). |
| Research protocol | `backtest/research.py` | 70/15/15 split, dev-only grid search, rolling walk-forward, 13 stress scenarios, ±20% parameter robustness, verdict. |
| Funding ingest | `ingest/funding.py` | `fundingHistory` paging (500/page), strict parsing, append-only `funding_rates`. |
| Storage | migration `0006_backtest`, `backtest/store.py` | `funding_rates`, `backtest_runs`, `backtest_trades`; UPDATE/DELETE rejected by trigger; least-privilege grants. |
| API (GET only) | `api/backtest.py` | `/api/backtest/strategies`, `/api/backtest/runs`, `/api/backtest/runs/{id}`. |
| CLI | `cli.py` | `autotrader funding-sync`, `autotrader backtest [--strategy …] [--out-dir …] [--no-save]`. |
| Hebrew UI | `apps/web/app/backtest/*` | Backtest Lab: strategy picker with verdicts, verdict + criteria, KPIs, candlestick chart with entry/exit markers and stop/target lines for a selected trade, equity curve, detailed trade history (costs, risk, rationale), segments, walk-forward, stress tests, blocked signals, data/assumptions/reproducibility. |

Decisions: ADR-022 … ADR-027 in `docs/DECISIONS.md`.

## 2. Data used (all real, all from Hyperliquid mainnet)

| Series | Bars / records | Range (UTC) | Missing |
|---|---|---|---|
| `xyz:CL` 30m (signals) | 5,000 closed | 2026-07-23 04:00 → 2026-11-04 08:00 (≈104 days) | 0 |
| `xyz:CL` 1h (trend filter) | 5,000 closed | 2026-04-10 → 2026-11-04 (≈208 days) | 0 |
| Funding (hourly) | 7,242 | 2026-01-06 15:00 → 2026-11-04 08:00 | 0 hours |

Data fingerprint `f869a045c247c23c…`. The 5,000-bar API limit is the binding constraint (ADR-006); nothing older was used or synthesized.

## 3. Results (pre-registered parameters, after all costs, 1,000 USDC virtual)

| | EMA 20/50 + RSI | Trend Following | Breakout + Volume |
|---|---|---|---|
| **Verdict** | **FAIL** | **FAIL** | **INCONCLUSIVE** |
| Trades (long/short) | 35 (17/18) | 45 (24/21) | 49 (27/22) |
| Win rate | 17.1% | 24.4% | 30.6% |
| Profit factor | 0.32 | 0.50 | 0.73 |
| Gross P&L before costs | −19.96 | −17.01 | **+10.07** |
| Fees / spread / slippage | 16.07 / 1.53 / 7.93 | 16.52 / 1.45 / 7.37 | 22.81 / 1.93 / 9.92 |
| Funding (+ = paid) | +0.78 | −2.82 | −2.78 |
| **Net P&L** | **−46.26 (−4.63%)** | **−39.53 (−3.95%)** | **−21.79 (−2.18%)** |
| Max drawdown (conservative) | 4.75% | 5.18% (halt hit) | 3.41% |
| Expectancy / trade | −0.54 R | −0.36 R | −0.18 R |
| 95% bootstrap CI | [−0.82, −0.21] | [−0.63, −0.04] | [−0.49, +0.15] |
| Longest losing streak | 17 | 8 | 9 |
| Time in market | 8.1% | 19.6% | 13.7% |
| Sharpe (daily, annualised) | −5.4 | −4.3 | −2.0 |

**Out-of-sample checks**

| | EMA 20/50 + RSI | Trend Following | Breakout + Volume |
|---|---|---|---|
| Segments dev / val / holdout (net USDC, trades) | −19.50 (21) / −14.23 (6) / −13.25 (8) | −39.53 (45, halted) / −12.71 (8) / −7.26 (9) | −8.89 (31) / −0.13 (8) / −12.89 (10) |
| Dev-optimised params on holdout | −1.05 (9) | −8.12 (8) | −8.19 (8) |
| Walk-forward OOS (4 folds) | 24 trades, −23.62, −0.39 R | 33 trades, −50.95, −0.62 R | 23 trades, −33.90, −0.59 R |
| Net at 0.5× / 2× costs | −39.79 / −46.88 | −36.19 / −42.53 | −8.46 / −47.54 |
| Net at 10× slippage shock | −51.45 | −41.39 | −51.01 |
| Without CME session gate | −50.51 | −33.22 | −45.44 |

Interpretation:
- **EMA 20/50 + RSI** and **Trend Following** lose even before costs; the 95% CI of expectancy is entirely below zero ⇒ FAIL on this sample.
- **Breakout + Volume** has a small positive gross result, but costs (mostly the assumed 0.10% fee per fill) turn it negative. The CI includes zero, the holdout and walk-forward are negative, and it does not survive 2× costs ⇒ INCONCLUSIVE, with no evidence of an edge.
- Optimising on development did not carry over to holdout for any strategy (classic overfitting signature).
- No bar touched both stop and target in any run, so the ambiguity rule did not change results here (it is still tested on synthetic data).
- The session gate helps the breakout strategy (−45 → −22 USDC): entries while the WTI reference is closed are worse.

## 4. How correctness is enforced

- **Look-ahead:** indicators and every strategy decision are prefix-invariant on the real data (future bars removed or replaced with arbitrary values ⇒ identical decisions); trades that ended before T are identical when the data after T is deleted; trend bars are matched by `close_time ≤ decision time`; funding is loaded `as_of`; the auditor re-checks entry ≥ signal time on every run.
- **Unrealistic results:** reference prices must lie within the traded bar; fills can only be worse than the reference; target needs a trade-through; exact `Decimal` identity `net = gross − spread − slippage − fees − funding` per trade and for final equity; a seeded random-walk test must not produce a statistically positive edge for any strategy.
- **Risk:** planned loss ≤ 0.25% of equity including costs, leverage ≤ 2×, ≤ 4 entries/day, one position, daily loss, drawdown halt, consecutive-loss pause — each tested.
- **Determinism:** two runs on the same data produce the same `report_sha256`.

## 5. Tests and checks (commands run)

```
uv run ruff check src tests && uv run ruff format --check src tests   -> All checks passed
uv run mypy                                                         -> no issues (57 files)
uv run pytest --cov=autotrader  (with PostgreSQL 16 *_test DB)      -> see TEST_RESULTS.md
npm run lint && npm run typecheck && npm run build                  -> clean
```

New test files: `test_indicators.py`, `test_sessions.py`, `test_strategies.py`, `test_engine.py`, `test_research.py`, `test_funding_and_backtest_api.py`, plus PostgreSQL tests for migration 0006 (immutability, append-only funding, model/migration drift). Real recorded inputs: `tests/fixtures/*_2026-11-04*.json.gz`.

## 6. Risks and limitations

1. **Sample size.** ≈104 days of 30m data and 35–49 trades per strategy. One market regime. A PASS is impossible by design (≥ 180 days required) — the binding limit is the exchange's 5,000-bar history.
2. **Fees are an assumption** (0.10% per fill). The real HIP-3 taker fee for `xyz:CL` could not be verified from this environment; if it is lower, breakout's cost drag shrinks (0.5× costs still loses 8.46 USDC).
3. **Spread/slippage** are modelled constants, not historical book data.
4. **Session calendar is UNVERIFIED** (CME pages unreachable). Holidays are blocked for the whole day conservatively.
5. **Funding notional** uses the candle close, not the (unavailable) historical oracle price.
6. Intrabar order is unknown; stop-first is conservative but not proof.
7. Backtest risk rules differ from Phase 3 in one place: consecutive-loss pause ends at 00:00 UTC instead of manual review.
8. Results are research, not trading advice, and do not justify live trading.

## 6b. Independent code review (separate reviewer agent)

No HIGH or MEDIUM findings. Six LOW findings, all fixed in the second commit (primary results unchanged; two stress figures moved by < 0.4 USDC):

1. Evening session before full CME closures (Thursday night before Good Friday, Christmas/New Year eves) was treated as open → closed now.
2. Off-hours cost multiplier ignored holidays (slightly optimistic) → holiday-aware `reference_trading()`.
3. CL expiry used the conservative holiday list, so December expiries were off by 1–3 days → separate exchange business-day calendar.
4. Zero-volume check used the fill bar's volume (known only after the fill) → uses the signal bar.
5. Missing funding feed let entries through the abnormal-funding gate → `FUNDING_DATA_MISSING` blocks (fail closed).
6. Walk-forward train windows overlap the holdout segment → stated in the report; the holdout criterion uses only the dev-locked parameters.

## 6c. Research & reliability audit (pre-merge, 2026-11-04)

**1. Data separation / holdout.** The dev-only optimisation never used validation or holdout. However, walk-forward folds in the earlier PR #3 runs **trained on (selected parameters on) holdout data**, and holdout results were reported. Fixed: walk-forward now stops at the holdout start (ADR-029). The holdout of this dataset is still **EXPOSED**: **there is currently no untouched independent test.** The verdict can no longer PASS on this dataset (`holdout_untouched` criterion). Only forward data after 2026-11-04 08:00 UTC can serve as a clean test.

**2. Fees.** The docs site is unreachable from this environment, so the fee was measured from the exchange's own public records (ADR-028). The actual tier-0 taker fee on `xyz:CL` is **0.0090%** (0.2 × the 0.045% tier rate, growth mode). The old assumption was 0.10%, about 11× higher. Jul–Aug 2026 was not observed, and growth mode can end, so the fee scenarios are reported.

**3. Trading hours.** `xyz:CL` traded in every 30m bar for 104 days, weekends and holidays included. CME hours are a strategy filter for entries only; exits are never blocked (ADR-030).

**4. Re-run on the identical dataset** (`--as-of 2026-11-04T08:00:05Z`, fingerprint `f869a045…`, commit `bd10502`, 0 audit violations). Strategy parameters are unchanged. Nothing was optimised on the holdout.

| | EMA 20/50 + RSI | | Trend Following | | Breakout + Volume | |
|---|---|---|---|---|---|---|
| | before | **after** | before | **after** | before | **after** |
| Verdict | FAIL | **FAIL** | FAIL | **INCONCLUSIVE** | INCONCLUSIVE | **INCONCLUSIVE** |
| Trades | 35 | 35 | 45 | 51 | 49 | 49 |
| Win rate | 17.1% | 17.1% | 24.4% | 23.5% | 30.6% | 30.6% |
| Profit factor | 0.32 | 0.44 | 0.50 | 0.60 | 0.73 | 1.00 |
| P&L before costs | −19.96 | −23.57 | −17.01 | −25.50 | +10.07 | +13.79 |
| Total costs | 26.30 | 14.23 | 22.51 | 10.67 | 31.86 | 13.77 |
| — of which fees | 16.07 | 1.77 | 16.52 | 2.02 | 22.81 | 2.55 |
| **Net P&L** | −46.26 | **−37.80** | −39.53 | **−36.17** | −21.79 | **+0.02** |
| Max drawdown | 4.75% | 4.39% | 5.18% | 5.08% (halt) | 3.41% | 2.73% |
| Expectancy (R) | −0.54 | −0.44 | −0.36 | −0.29 | −0.18 | 0.00 |
| 95% CI (R) | [−0.82, −0.21] | [−0.78, −0.03] | [−0.63, −0.04] | [−0.56, +0.04] | [−0.49, +0.15] | [−0.37, +0.41] |
| Validation (trades, net) | 6, −14.23 | 6, −14.07 | 8, −12.71 | 8, −11.78 | 8, −0.13 | 8, +4.10 |
| Holdout, pre-registered params (exposed) | 8, −13.25 | 8, −11.57 | 9, −7.26 | 9, −5.18 | 10, −12.89 | 10, −9.53 |
| Holdout, dev-optimised params | 9, −1.05 | 9, +2.17 | 8, −8.12 | 8, −6.74 | 8, −8.19 | 8, −4.86 |
| Walk-forward OOS (folds, trades, net, R) | 4, 24, −23.62, −0.39 | 3, 17, −16.45, −0.39 | 4, 33, −50.95, −0.62 | 3, 27, −31.77, −0.47 | 4, 23, −33.90, −0.59 | 3, 15, −25.19, −0.67 |
| Net at 2× costs | −46.88 | −39.02 | −42.53 | −37.92 | −47.54 | −21.38 |
| Net at fee 0.045% / 0.09% / 0.10% | — | −41.58 / −45.49 / −46.26 | — | −32.30 / −38.51 / −39.53 | — | −9.81 / −19.84 / −21.79 |

Why figures moved:
- **Primary results:** only the fee changed. With the 0.10% fee, the new code reproduces the old results exactly.
- **Gross P&L moved too,** because position size is set from risk *including* costs: lower fees allow slightly larger positions.
- **Trend Following has more trades** (45 → 51) because its 5% drawdown halt came later.
- **Walk-forward:** it now has 3 folds ending before the holdout, instead of 4 folds that reached into it.

Interpretation:
- **Breakout + Volume** breaks even after realistic fees (+0.02 USDC on 49 trades). It loses on the holdout, in walk-forward OOS, at 2× costs and at any fee ≥ 0.045%. It shows no edge, and the verdict stays INCONCLUSIVE.
- **Trend Following** moves from FAIL to INCONCLUSIVE only because the CI's upper end now crosses 0 (+0.04 R). It still loses before costs, in every segment and in walk-forward.
- **EMA 20/50 + RSI** is still FAIL: it loses before costs and the CI is entirely below 0.
- Costs are now dominated by the **0.05%/side slippage assumption**, not by fees.

## 7. Security review

- No new network destinations: `fundingHistory` was already on the Info allow-list; all traffic still goes through `InfoClient` (POST `/info` only).
- No new routes that change state: all new API routes are GET; the web proxy allows only `backtest/runs`, `backtest/strategies` and `backtest/runs/<digits>`.
- No wallet, key or signing code; `autotrader safety-check` passes; tests block external network.
- DB: new tables are SELECT for the API role, SELECT+INSERT for the worker role, immutable by trigger.

## 8. How to run

```bash
uv run alembic upgrade head
uv run autotrader backfill
uv run autotrader funding-sync
uv run autotrader backtest
uv run uvicorn autotrader.api.main:app --port 8000
cd apps/web && API_BASE_URL=http://localhost:8000 npm run dev   # http://localhost:3000/backtest
```

Screenshots: `docs/screenshots/phase2-backtest-iphone.png`, `docs/screenshots/phase2-verdict.png`, `docs/screenshots/phase2-trade-focus.png`.
