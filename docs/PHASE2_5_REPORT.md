# Phase 2.5 Report — Forward Testing & Strategy Validation (xyz:CL, PAPER-ONLY)

**Date:** 2026-11-04 · **Branch:** `claude/keen-curie-78advu` (restarted from `main` @ `5eab32b`, Phase 2 merged)
**Scope:** test the frozen Phase 2 strategies on market data that did not exist when they were defined. Simulation only: no wallet, no keys, no orders.

> **סיכום בעברית:**
> - **הקפאה:** כל אסטרטגיה מוקפאת לפני תחילת הבדיקה: גרסה, פרמטרים, עלויות, מגבלות סיכון וטביעת אצבע של הקוד.
> - **רישום בזמן אמת:** על כל נר 30 דקות שנסגר נשמרת החלטה ברשומה שלא ניתן לשנות או למחוק. הרשומה כוללת חותמת זמן, נתוני שוק, גרסה וסיבה.
> - **חישוב התוצאות:** רווח והפסד וירטואליים מחושבים מחדש בכל פעם מתוך הרשומות. החישוב משתמש באותו מנוע כמו ה־Backtest, עם עמלות, Spread, Slippage ו־Funding.
> - **מניעת שינוי בדיעבד:** שינוי בקוד האסטרטגיה עוצר את הבדיקה. נתונים שתוקנו בדיעבד מסמנים את התוצאה כ־INVALID.
> - **מסקנות:** התוצאה תישאר INCONCLUSIVE עד שיהיו לפחות 30 עסקאות סגורות, 30 ימים ו־15 ימים שונים עם עסקה. זה דורש להריץ את שירות ה־forward ברציפות על תשתית קבועה.

## 1. What was built

| Component | Location | Role |
|---|---|---|
| Freeze | `forward/freeze.py` | Builds and hashes the frozen config: strategy, params (hash re-verified on load), cost model, risk, execution, thresholds, history anchor, start time, reference backtest. `code_hash` covers every file that turns config + data into decisions, fills and verdicts. |
| Recorder | `forward/recorder.py` | `create_test` (one active test per strategy, advisory lock), `tick` (one decision per newly closed 30m bar, chronological, idempotent), `stop_test`, integrity halt, gap/stale events, 1h-bar readiness wait. |
| Evaluator | `forward/evaluate.py` | Recomputes results from the journal with the backtest `Engine` (`live_edge`). Also: parity check, backtest comparison, 2× cost and funding stress, forward verdict. |
| Worker | `forward/worker.py`, compose service `forward` | Ticks every 30 s and syncs recorded funding every 10 min. One failing test doesn't block the others. |
| Engine | `backtest/engine.py` | `live_edge=True`: the open position stays open, and a last-bar signal is pending. Ordinary backtests are byte-identical to Phase 2 (verified). |
| Schema | migration `0007_forward_tests` | `forward_tests`, `forward_decisions`, `forward_events`. Immutable (UPDATE/DELETE/TRUNCATE rejected). A decision before `starts_at` is rejected, and so is one made before its bar closed. TRUNCATE is now also blocked on the Phase 2 backtest tables. |
| API | `api/forward.py` | GET-only: `/api/forward/tests`, `/tests/{id}` (full recomputed report), `/tests/{id}/decisions`. |
| CLI | `cli.py` | `autotrader forward freeze / tick / run / status / stop`. |
| Dashboard | `apps/web/app/forward/*` | Verdict and progress toward the thresholds, virtual P&L, win rate, drawdown, profit factor, open/pending position, chart with entries/exits, equity curve, Backtest-vs-Forward table and parity, full signal history (outcome, market snapshot, rationale, late flag), frozen config and events. |

Design decisions: ADR-031 (and ADR-029 on why the old holdout can't serve as an independent test).

## 2. How "no retroactive changes" is enforced

| Risk | Control |
|---|---|
| Changing params mid-test | Params live only in the immutable `forward_tests.config`; on load, the hash must reproduce. |
| Changing strategy/engine code mid-test | `code_hash` is checked on disk and against the code loaded by the running worker; a mismatch → `INTEGRITY_HALT`, nothing more is recorded, verdict INVALID. |
| Editing or deleting signals | DB triggers reject UPDATE/DELETE/TRUNCATE. Parity recomputes every decision, so a forged row is INVALID. |
| Back-filling decisions for the past | DB trigger: no decision for a bar before `starts_at`. CHECK `evaluated_at >= bar_close_time`. |
| Editing results | Results are never stored; they are recomputed from immutable inputs. |
| Revised candles or late gap fills | Each decision stores an input digest; a changed input ⇒ `INPUT_DATA_CHANGED` ⇒ INVALID. |
| Restarting until it looks good | One active test per strategy. A stopped/halted test can never PASS. Every test ever frozen per strategy is counted in the report. |
| Waiting out the clock | "Days" count recorded bars, not wall-clock time. |

## 3. Comparison with the backtest

- **Parity:** the backtest code path recomputes each live decision on the same closed bars. It must match 100%.
- **Consistency:** forward vs the frozen reference backtest (Phase 2 runs 7–9, same parameter hash):
  - expectancy (R), win rate, profit factor;
  - trades and signals per day (same definition as the backtest);
  - max drawdown.
  "Consistent" means the backtest expectancy lies in the forward 95% bootstrap CI, judged only with ≥ 10 forward trades.

## 4. Verdict rules (frozen per test)

- **INVALID:** a code change, a parity/input mismatch or an audit violation.
- **INCONCLUSIVE:** fewer than 30 closed trades, 30 days of recorded bars, or 15 distinct trade days.
- **PASS:** the expectancy CI is above 0 and the strategy is still profitable at 2× costs (fees, spread, slippage and funding), and the test is still running.
- **FAIL:** the CI is entirely below 0.

## 5. Evidence

- **Replay of the recorded mainnet data as if live** (tests, freeze at 2026-10-10 09:07 UTC, 20 days, ticks every 5 h):
  - 959 decisions, 26 signals, 11 closed trades, net +4.43 USDC;
  - verdict INCONCLUSIVE (11/30 trades, 20/30 days, 7/15 trade days);
  - parity 959/959, audit 0 violations.
  The recorded journal equals a recomputation over the full dataset, including later "future" data: no look-ahead, and tick cadence doesn't matter.
- **Live in this environment:** see §7.
- **Ordinary backtests are unchanged:** a Phase 2 re-run on the identical dataset (`f869a045…`) gives reports identical to runs 7–9, apart from the commit hash.

## 6. Independent review (separate reviewer agent) — all fixed

| Sev. | Finding | Fix |
|---|---|---|
| HIGH | A 30m bar on the hour could be decided before the collector stored the matching 1h bar (two separate commits; observed 0.2 s apart live). The live decision would be `TREND_DATA_MISSING` while the replay sees the 1h bar ⇒ parity INVALID. | Recorder waits for that 1h bar (≤ 15 min, then records a DATA_GAP). Tested. |
| MED | `observed_days` grew with wall-clock time (collector down / test stopped). | Counts recorded bars only. |
| MED | Optional stopping: a stopped test could still PASS; concurrent freezes could create two active tests. | Stopped/halted ⇒ never PASS; advisory lock (tested with 2 threads on PostgreSQL); tests per strategy reported. |
| MED | Forward signals/day counted signals while in a position (not the backtest's definition). | Uses the engine's `SIGNAL` count. |
| MED | Revised candles or late gap fills changed P&L silently. | Input digest re-checked ⇒ INVALID. Funding syncs every 10 min, so recent hours settle quickly. |
| LOW | `code_hash` missed evaluation/validation/data code; disk-only check. | Wider file list; the running worker's loaded hash is checked too. |
| LOW | One test's exception aborted the cycle; 2× stress ignored funding; TRUNCATE allowed. | All fixed. |

Remaining (documented):
- Every API request recomputes the report from the anchor. That is fine for months of data and can be cached later.
- Funding for the most recent ~10 minutes may still arrive, so the last hour's P&L is provisional.

## 7. Live run in this environment (2026-11-04)

Frozen at 10:12 UTC (tests #1–#3, start 10:30), with the collector and forward worker running against Hyperliquid mainnet; results in the final report of this phase. This cloud container is ephemeral. **The real 30–60 day forward observation must run on persistent infrastructure:**

```bash
docker compose up -d db migrate collector forward api web
```

```bash
docker compose run --rm forward autotrader forward freeze
```

## 8. Tests

```
ruff / ruff format / mypy                      -> clean
pytest (PostgreSQL 16)                         -> 432 passed, 4 skipped (live, opt-in)
npm run lint / typecheck / build               -> clean (routes /, /backtest, /forward)
```
New: `tests/unit/test_forward.py` (24 tests), plus PostgreSQL tests for the journal triggers, TRUNCATE, the start-time rules and concurrent freezing.

## 9. Security

- No new network destinations. The worker reads the database and calls only the public Info API (`fundingHistory`) through the existing allow-listed client.
- All new API routes are GET. The web proxy allows only `forward/tests`, `forward/tests/<id>` and `forward/tests/<id>/decisions`.
- No wallet, key or order code. `autotrader safety-check` passes.

---

## 10. Live resilience run on Hyperliquid mainnet (2026-11-04, 10:28–12:32 UTC)

**Setup:**
- Cloud dev container with PostgreSQL 16, the market-data collector (WS + REST), the forward worker and the API.
- Forward tests **#4–#6** were frozen at 10:28:39 UTC and started at 10:30 UTC.
- A verified backup (`pg_dump` + `pg_restore --list` + sha256) was taken before the first outage.
- Every outage was time-bounded and scripted. Every check was read-only (SQL plus the GET API) and logged with timestamps.
- **No frozen code was changed during the run.**

| # | Test | Window (UTC) | Result | Evidence |
|---|---|---|---|---|
| A | Real-time decision on the first bar (an on-the-hour close) | 11:00 | **PASS** | 3 decisions at 11:00:45, `late=false`; the 1h-bar wait worked (no `TREND_DATA_MISSING`); health 200 `OK` |
| B1 | Forward-worker outage detected | 11:02:02 stop → 11:12:51, 11:32:04, 11:33:38 | **PASS** | `/api/forward/health` HTTP 503 `FORWARD_WORKER_DOWN` at every check; last heartbeat 11:02:02, state `STOPPED` (clean SIGTERM) |
| B2 | No decisions while the worker is down | 11:02–11:42 | **PASS** | the 11:00 bar was closed in the DB at 11:33:38 but undecided; the count stayed 3 |
| B3 | Recovery after the worker restart | 11:42:04 → 11:44:06 | **PASS** | the 11:00 bar was decided at 11:42:05 with **`late=true`**, one row per test; ordered = true; duplicates 0, pre-start 0, look-ahead 0; parity 2/2, 0 mismatches; real-time 1 / late 1 per test; health 200 `OK` |
| C1 | Collector outage detected | 11:45:04 stop → 11:57:27, 12:02:00 | **PASS** | HTTP 503 `COLLECTOR_STOPPED` (+ `DATA_STALE` 30m/1h at 12:02); heartbeat `STOPPED`, WS `DISCONNECTED` |
| C2 | No data invented during the outage | 12:02:00 | **PASS** | the 11:30 bar (closed on the exchange at 12:00) remained `closed=false` in the DB; no decision for it |
| C3 | Recovery after the collector restart | 12:05:01 → 12:06:31 | **PASS** | gap filled from REST (`src=rest`) for the 30m 11:30 and 1h 11:00 bars; the bar was decided at 12:05:27; health 200 `OK` |
| D1 | Forward worker survives a PostgreSQL restart | 12:10:03 | **PASS** | worker alive at 12:10:23; it kept recording (12:00 bar decided at 12:30:21) |
| D2 | Collector after a PostgreSQL restart | 12:10:02 → 12:11:56 | **PASS (fail-closed) + manual restart** | the collector stopped itself (`single-writer lock lost`, ADR-020: never two writers). This container has no process supervisor, so it was started once by hand, emulating `restart: always` (configured in `docker-compose.prod.yml`). After that: `RUNNING`, WS `CONNECTED`, health `OK`, no data loss. **Automatic restart was not tested here; it must be verified on the server.** |
| E1 | Real-time decision after all outages | 12:30:21 | **PASS** | 12:00 bar decided `late=false`; **first live signal: test #6 Breakout + Volume SHORT** (downtrend + breakdown + volume; mark 94.748 / oracle 94.740), pending entry at the next bar |
| E2 | Overall integrity | 12:31:30 | **PASS** | duplicates 0; decisions before the start 0; decisions before their bar closed 0; no DATA_GAP or DATA_STALE event for tests #4–#6; all closed bars present |
| E3 | Live/replay parity on PostgreSQL | 12:31:30 | **FAIL → fixed** | test #6 showed 1 mismatch on its SIGNAL bar, which made the test INVALID. See §10.1. |

**Forward decisions recorded live, after the freeze (tests #4–#6; the historical replay is not included):**
- 12 decisions in total: **9 in real time** (`late=false`) and **3 late** (`late=true`, the 11:00 bar decided after the worker outage);
- 1 signal (SHORT, test #6);
- 0 duplicates.

The 11:30 bar was decided 5 min 27 s after it closed because the collector was down. That is below the 10-minute `late` threshold, so it counts as real time by definition.

Tests #1–#3 (frozen 10:12) were stopped at 10:20:24 with **`INTEGRITY_HALT`** when code under the frozen hash changed on disk. They are kept and labelled, never deleted, as designed.

### 10.1 Bug found by the live run: NUMERIC rounding broke parity on PostgreSQL (fixed)

**Symptom.** Test #6's first live SIGNAL produced a parity mismatch, so its verdict became INVALID.

**Cause.** `forward_decisions.atr` is `NUMERIC(38,18)`. PostgreSQL rounded the stored ATR to `0.689650506380416124`, while the replay computed `0.6896505063804161241237483115980913`, and parity compared the two exactly. No-signal decisions have no ATR, which is why phases A–D didn't show it. The unit tests use SQLite, which stores exact decimal text, so they couldn't see it. **Every forward test on PostgreSQL would have become INVALID at its first signal.**

**Fix:**
- `freeze.stored_decimal()` applies the column scale with PostgreSQL's rounding (half away from zero).
- The recorder stores values at that scale, and parity compares at it.

**Proof:**
- New end-to-end test `test_forward_replay_parity_on_postgresql`: 4 days of recorded mainnet data replayed into PostgreSQL.
  - **Without the fix:** 4 mismatches out of 191 decisions, all on SIGNAL bars.
  - **With the fix:** 0 mismatches.
- Running the fixed code read-only against the live database: tests #4–#6 parity 4/4 each, 0 mismatches.

**Effect on the running tests.** The fix changes code under the frozen hash. It was therefore made in a separate worktree and **not deployed to the running test**, so the live evidence stays as recorded. Once it is deployed, tests #4–#6 will stop with `INTEGRITY_HALT` (kept, labelled), and the official forward tests must be frozen **on the persistent server after this PR is merged** (`docs/DEPLOYMENT.md` §3 step 6).

### 10.2 Not tested here (must be verified on the server)

- Automatic restart by Docker (`restart: always`) after a crash or a PostgreSQL restart.
- Reboot of the whole machine.
- The external dead-man's-switch alert (healthchecks.io).
- Nightly backups over several days, and the off-site copy.
- A forward observation of 30+ days.

This cloud container is ephemeral and can't demonstrate 24/7 operation.

**Tests after the fix:**
- 439 passed, 4 skipped (the live tests run separately);
- ruff, ruff format and mypy clean.
