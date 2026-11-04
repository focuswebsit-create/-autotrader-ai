# Phase 1 Report — Market Data Engine (xyz:CL, PAPER-ONLY)

**Date:** 2026-11-03 (updated 2026-11-04: overnight outage §4.2, final review §7b, data-integrity audit §7c) · **Branch:** `claude/phase1-market-data` · **PR:** #2 (stacked on PR #1 / Phase 0)
**Scope:** historical + live market data from Hyperliquid **mainnet** (read-only). No strategy, no execution engine, no orders of any kind.

> **סיכום בעברית:** מנוע הנתונים אוסף נרות של 30 דקות ושל שעה עבור `xyz:CL` מ־Hyperliquid Mainnet, בקריאה בלבד.
> - **אמינות הנתונים:** כל 10,000 הנרות הסגורים שנבדקו זהים לחלוטין למקור. אין כפילויות, ואין נרות פתוחים שנשמרו כסופיים.
> - **סגירת נרות:** נרות נסגרים אוטומטית מ־REST כשבע וחצי שניות אחרי סוף התקופה.
> - **התאוששות מניתוק:** בבדיקה חיה ומבודדת המערכת עמדה בניתוק רשת של 60 שניות, התחברה מחדש והשלימה את הפער מ־REST, בלי לאבד נתונים ובלי ליצור כפילויות.
> - **אבטחה:** כל ניסיונות הפריצה נחסמו. ה־API ו־ה־WebSocket הם לקריאה בלבד, ומשתמשי מסד הנתונים פועלים עם הרשאות מינימליות.

---

## 1. What was built

| Component | Location | What it does |
|---|---|---|
| Candle model + validation | `market/candles.py` | Exact `Decimal` parsing. Rejects (never repairs) malformed values, floats/NaN, non-positive or out-of-range prices, OHLC inconsistency, negative volume, misaligned times, wrong market. A bar counts as closed only at `close_time + 5 s`. |
| Series quality checks | `market/quality.py` | Duplicates (identical vs conflicting), gaps (count of missing bars), outliers (flagged, not removed). |
| Schema (migration 0003) | `db/models.py`, `migrations/versions/0003_market_data.py` | `candles` (unique `(coin, interval, open_time)`, OHLC/positivity CHECKs), `data_quality_issues`, `ingestion_runs` (provenance), `service_heartbeats`; least-privilege roles. |
| Storage rules | `db/market_data.py` | Idempotent upserts. Closed bars are immutable (a REST revision is recorded, not applied). Out-of-order WS updates are ignored. Gaps are recorded, never filled, and resolved only by real data. `closed_candles(as_of)` gives a look-ahead-free read. Single-writer advisory lock. |
| REST ingestion | `ingest/rest.py` | Full backfill (`startTime=0` → whatever the API has), chunked sync after the last closed bar, finalize. Every call is stored with requested vs received range, counts and sha256. |
| Live collector | `ingest/live.py`, `collector_main.py` | Guarded WS (`candle` 30m/1h + `activeAssetCtx`), stale detection (45 s), jittered exponential backoff (1→60 s), REST gap fill after every (re)connect, finalize every 30 s, heartbeat every 10 s, graceful SIGTERM, lock-loss guard. |
| WS guard | `common/ws.py`, `common/safety.py` | Exact URL only. Outgoing frames limited to `subscribe`/`unsubscribe`/`ping` for public channels; `post` and user channels are refused before sending. |
| API (GET only) | `api/market_data.py` | `/api/candles` (closed bars + separate `forming`), `/api/data/report`, `/api/data/issues`, `/api/collector/status` (alerts derived from stored facts). |
| CLI | `cli.py` | `autotrader backfill`, `collect`, `data-report`. |
| DB users | `infra/db/roles.sql`, `init-roles.sh` | `autotrader_api` (SELECT only), `autotrader_worker` (INSERT/UPDATE on market-data tables only). |
| Dashboard (Hebrew RTL) | `apps/web/app/components/*` | Candlestick chart (30m/1h, date presets and custom UTC range, OHLCV readout, forming bar in gray, gap markers, volume), live collector status and alerts, data quality and coverage, ingestion history. Same-origin GET-only proxy with a path allow-list. |

Screenshots: `docs/screenshots/phase1-desktop.png`, `docs/screenshots/phase1-mobile.png`.

## 2. Data collected (real mainnet data, `autotrader_dev`, 2026-11-03 16:11 UTC)

| Interval | Closed bars stored | First bar (UTC) | Last closed bar (UTC) | Span | Missing bars | Forming bars |
|---|---|---|---|---|---|---|
| 30m | **5,008** | 2026-07-22 08:00 | 2026-11-03 15:30 | ~104.3 days | **0** | 1 |
| 1h | **5,004** | 2026-04-09 04:00 | 2026-11-03 15:00 | ~208.4 days | **0** | 1 |

- **Source:** `POST https://api.hyperliquid.xyz/info` (`candleSnapshot`) and `wss://api.hyperliquid.xyz/ws` (`candle`). The market `xyz:CL` (dex `xyz`, index 1) was resolved at runtime on every start.
- **History limit:** the API serves only the most recent ~5,000 bars per interval. It returned 5,008/5,004 at 15:43 and exactly 5,000/5,000 later. The DB keeps everything already collected, which is why it holds 8 and 4 bars older than the API's current window. Nothing was synthesized.
- **Data-quality issues recorded:** **0** (no gaps, rejected bars, conflicting duplicates, revisions or outliers in the real data).
- **Ingestion runs:** 18, **0 failed**.

## 3. Verification against the real source

Script: every stored closed bar was compared field by field (O/H/L/C/V/trades, exact `Decimal`) with a fresh `candleSnapshot` (16:02 UTC).

| Check | 30m | 1h |
|---|---|---|
| Closed bars compared with the API | 5,000 | 5,000 |
| Value mismatches | **0** | **0** |
| API closed bars missing in DB | 0 | 0 |
| Duplicate `(coin, interval, open_time)` | 0 | 0 |
| Bars marked closed before `close_time + 5 s` | 0 | 0 |
| Forming bars that should already be closed | 0 | 0 |

**Automatic closing via REST (observed live):** at the 16:00 boundary the 30m bar (15:30) and the 1h bar (15:00) changed from forming (`source=ws`) to `is_closed=true, source=rest` at **16:00:07 UTC** (ingestion runs #13/#14, `kind=finalize`). The new 16:00 bars appeared as forming from the WS.

## 4. Disconnect / recovery tests

### 4.1 Live, isolated, time-bounded network outage (2026-11-03 16:05–16:08 UTC)
**Setup:**
- A second collector on a **separate DB** (`autotrader_chaos`), connected only through a local TCP relay to the egress proxy.
- Killing the relay = network outage. Hard limit of 240 s.
- The main collector (different DB and network path) was not touched; it had no reconnects during the test.

| Time (UTC) | Event | Collector state |
|---|---|---|
| 16:05:44 | Connected, backfill done (5,001 + 5,001 received) | `RUNNING/CONNECTED` |
| 16:05:54 | **Network cut** (relay SIGKILL) | — |
| +10 s | Disconnect detected; retries with backoff | `DEGRADED/DISCONNECTED`, 4 reconnect attempts |
| +60 s | Still cut; no crash; error recorded | `DEGRADED/DISCONNECTED`, 7 attempts |
| 16:06:56 | **Network restored** | — |
| 16:07:34 | Reconnected (38 s: waiting out the current backoff, capped at 60 s) | `RUNNING/CONNECTED` |
| 16:07:34+ | REST gap fill after reconnect: `gap_fill OK` for 30m and 1h | — |
| 16:07:43 | SIGTERM → clean stop, exit code 0 | `STOPPED/DISCONNECTED` |

**After the test:**
- The test DB matched the main DB on all 5,000 + 5,000 common closed bars (0 differences).
- A direct REST comparison of the last 30 bars per interval gave 0 differences and 0 missing.
- 0 duplicates, 0 early closes.

**Not exercised in this 60-second test:** a bar closing *during* the outage. It was later exercised live by the unplanned ~14.5-hour outage (§4.2).

### 4.2 Unplanned ~14.5-hour outage (container reset, 2026-11-03 16:20 → 2026-11-04 06:54 UTC)
The development container was reset overnight. The main collector was killed **without** a graceful shutdown, so its last heartbeat row still said `RUNNING/CONNECTED`. This tested exactly the case §4.1 could not cover: many bars closed while the collector was down.

**Before the restart** (API, read-only user):
- `service_state = DOWN` (no heartbeat for 52,430 s); `COLLECTOR_DOWN` (ERROR).
- `DATA_STALE` for 30m and 1h; `fresh = false`.
- The stale `RUNNING` row was **not** trusted.

**After the restart** (06:54:39 UTC):
- Startup gap fill via REST: run #19 (30m) received 31, inserted **29**, finalized 1 forming bar; run #20 (1h) received 16, inserted **14**, finalized 1.
- Within 20 s: `RUNNING`, 0 alerts, `fresh = true`, 0 missing bars, 0 gaps.

**Checked against mainnet REST** (last 40 closed bars per interval):
- 30m: 40/40 identical (28 of them gap-filled after the restart), 0 missing, 0 left unclosed.
- 1h: 40/40 identical (13 of them gap-filled after the restart), 0 missing, 0 left unclosed.
- 0 duplicates, 0 bars closed early, 0 open data-quality issues.

### 4.3 Other live operational checks
- **Graceful restart** of the main collector, done twice: heartbeat `STOPPED`, then a restart with no duplicates (counts identical before and after).
- **Single writer:** a second `autotrader collect` against the same DB exited with **code 5** ("another collector is running").
- **REST rate limit:** heavy verification requests from this environment got HTTP 429. `InfoClient` retries 429 with backoff/`Retry-After`. The collector's own small requests were never rate-limited.

### 4.4 Automated tests (deterministic, run in CI)
`tests/unit/test_live_collector.py` (fake WS behind the real guard, recorded mainnet data):
- subscribe and ingest
- disconnect → reconnect → REST gap fill with no duplicates
- stale stream → recycled
- connect failures → backoff → recovery
- backoff bounds
- out-of-order frames ignored
- invalid frames recorded
- REST outage → `DEGRADED` without a crash
- database outage → no crash
- restart → no duplicates
- safety violation → collector stops

## 5. Problems found and fixed during Phase 1

| # | Problem | Fix | Regression test |
|---|---|---|---|
| 1 | SQLite stores NUMERIC as binary float (96.406 → 96.406000000000005912), producing false "revisions" in unit tests | Exact decimal-text type for SQLite only; OHLC CHECKs PostgreSQL-only | `test_market_data_db.py` |
| 2 | The reconnect loop caught *all* exceptions, so a `SafetyViolationError` would have reconnected instead of stopping | Safety violations propagate and stop the collector (also from the background sync loop) | `test_safety_violation_stops_collector` |
| 3 | Concurrent REST-sync and WS threads could race on the same bar (unique-key violation) | Single write lock for all candle writes | collector tests |
| 4 | The forming bar kept `source=rest` after WS updates | Source tracks the last writer of a forming bar | live check |
| 5 | API returned prices with 18 padded zeros | Exact normalisation (`94.595000…` → `94.595`) | `test_num_formatting` |
| 6 | Startup re-sync on a populated DB was labelled `backfill` | Labelled `gap_fill`; full backfill only on an empty DB | live check |
| 7 | **Lock session left "idle in transaction" for the collector's lifetime** (blocks VACUUM) | Dedicated AUTOCOMMIT connection for the advisory lock | `test_single_collector_lock` asserts no idle-in-transaction |
| 8 | **Closing the lock connection only returned it to the pool, so the lock stayed held** (the earlier test masked it via re-entrant reuse) | Non-pooled (`NullPool`) lock connection, explicit `pg_advisory_unlock_all()` and dispose | re-acquire from a new backend + `pg_locks` empty |
| 9 | If the DB dropped the lock connection, the collector would keep writing without the lock | Lock guard every 30 s; on loss the collector stops (exit 6) and must re-acquire on restart | `test_lock_loss_is_detected` |
| 10 | After recovery the UI still showed the old error as current | Current error vs "recovered from" error, both with timestamps | `test_disconnect_triggers_reconnect…` |
| 11 | React 19 lint: impure `Date.now()` in render; setState inside effects | Ranges computed at fetch time; polling started from timers | `npm run lint` |
| 12 | Mobile: cramped status grid, uneven range buttons | 1-column stats; 3×2 button grid | screenshots |

## 6. Security verification (PAPER-ONLY)

Active attempts against the running system (run at 16:09 UTC):

| Attempt | Result |
|---|---|
| REST order to `/exchange` via the app's HTTP client | **Blocked** before any bytes are sent |
| Signed action (`signature`/`action`) via `/info` | **Blocked** |
| Account data request (`clearinghouseState`) | **Blocked** (type not allow-listed) |
| Testnet / other host | **Blocked** |
| WS `post` (Hyperliquid's channel for signed actions) | **Blocked** |
| WS user channel (`orderUpdates`) / other WS URL | **Blocked** |
| Wallet key in env (`HL_PRIVATE_KEY`, or a 0x+64-hex value under any name) / `TRADING_MODE=LIVE` | **Startup refused** |
| `autotrader_worker`: DELETE / TRUNCATE candles, set `paper_only=false`, DROP TABLE | **permission denied / must be owner** |
| `autotrader_api`: INSERT / UPDATE | **permission denied** |
| POST/PUT/DELETE on the API; POST or non-allow-listed path via the web proxy | **405 / 404** |

**Static guarantees, all in CI:**
- No signing libraries in dependencies or the lockfile.
- Network-capable imports exist only in `common/http.py` (httpx/urllib) and `common/ws.py` (websockets).
- No `/exchange` literal outside the deny-list.
- Non-live tests cannot open external sockets.
- Every table is granted to both DB roles.

## 7. Test results (all actually run)

Local, Python 3.12 + PostgreSQL 16, with `RUN_LIVE_TESTS=1`:
- **ruff** clean · **mypy --strict** clean (41 files).
- **pytest after the data-integrity work: 308 passed** (unit + 28 PostgreSQL), 5 consecutive full runs green; live mainnet tests 4/4 (run after the review fixes). After the final review: 305 (301 + 4 live). Before the final review:
- **pytest: 286 passed**:
  - 262 unit: safety 90, discovery 41, WS guard 29, candles 25, API 15 + market API 13, collector 11, resilience 9, storage 8, static 8, REST ingest 7, CLI 6;
  - 20 PostgreSQL;
  - 4 live mainnet (discovery, candles 30m/1h, guarded WS stream).
- **Web:** ESLint ✔, TypeScript ✔, `next build` ✔.
- **Browser checks:** desktop 1366 px and iPhone 14; no horizontal scroll, no console errors, CSP intact.
- **CI on GitHub:** backend ✔, frontend ✔, docker ✔. The final commit's CI result is reported in the PR.

## 7b. Final independent review (2026-11-04) and fixes

An independent reviewer (no access to the author's conclusions) reviewed the full Phase 1 diff against `main`. **No critical issues**: no path to orders or signed actions, user channels or wallet keys. Every finding below was reproduced or checked, fixed, and covered by a regression test (`tests/unit/test_phase1_review_regressions.py`, plus PostgreSQL tests). The H1, M1 and M2 tests were also run against the *old* code and **fail there**, so they really catch the bugs.

| ID | Severity | Finding | Fix |
|---|---|---|---|
| H1 | High | A bar could be marked closed (and frozen) from a snapshot **requested before it closed** when the response was slow. Reproduced: request at 15:59:59, response at 16:00:06 → `is_closed=True` | "Closed" is decided by the time the request was **sent** (`closed_as_of`) |
| M1 | Medium | Reconnect backoff never reset after a successful session, so after ~7 drops (even days apart) every reconnect waited 30–60 s | Backoff resets after any session that connected |
| M2 | Medium | The write lock was held during the REST HTTP call, so a slow API stalled WS frame handling and forced stale-reconnect loops | Lock only around DB writes; separate sync lock; post-connect gap fill runs in the background |
| M3 | Medium | UI could show stale data as live: a leftover forming bar from a dead collector drawn as the current bar; "(live)" prices while the collector was down; out-of-order responses could show 30m under the "1h" button | API returns `forming` only if current (plus `stale_forming_open_time` and `fresh`); stale-data banner on the chart; "(last known)" + dimmed prices unless connected and recent; out-of-order responses dropped |
| M4 | Medium | After `run()` returned, a still-running sync thread could write after the STOPPED heartbeat and after the lock was released. `autotrader backfill` ignored the writer lock | Shutdown drains in-flight syncs and refuses new writes before STOPPED and before the lock is released; CLI backfill takes the lock (exit 5 if a collector runs) |
| L1 | Low | Closed-bar immutability was enforced only in code | Migration 0004: DB trigger rejects any UPDATE of a closed candle (verified as `autotrader_worker`) |
| L2 | Low | An older REST snapshot could rewind a forming bar updated by WS | Same volume/trade-count rule for REST unless REST is closing the bar |
| L3 | Low | A DB error while storing left the ingestion run RUNNING forever; made one collector test flaky | Store failure marks the run FAILED; flakiness gone (12/12 full runs green) |
| L4 | Low | Untimed rejections overwrote each other; REJECTED alerts never expired | One row per reason; the alert covers the last 24 h (history kept in the issue list) |
| L5 | Low | Trade count `n` accepted floats, bools and negatives | Strict non-negative int32 |
| L7 | Low | Lock-loss detection window of 30 s | 10 s |
| L8 | Low | Duplicate JSON keys could be read differently by the guard and by a server | Outgoing REST and WS payloads with duplicate keys are rejected |

**Fixed in the tests themselves:**
- The unit-test SQLite fixture used one in-memory connection shared by all threads, so one session's rollback could undo another thread's transaction. It now uses a file-backed DB with one connection per session, like PostgreSQL.
- A collector test now waits for the background gap fill it asserts on.

**Accepted, documented (no code change):**
- **L6:** values with more than 18 decimals would compare unequal after NUMERIC rounding. Hyperliquid prices have at most 6 decimals.
- **Lock DoS:** any DB role, including the read-only one, can take the collector's advisory-lock key and stop it from starting (exit 5). That needs DB credentials and can only *block* writes, never make them. PostgreSQL has no per-key permission for advisory locks.

**Deployed to the running dev system:** migration 0004 applied and the collector gracefully restarted. Closed bar counts were identical before and after, `RUNNING/CONNECTED`, no errors, `fresh=true`.

## 7c. Final data-integrity audit (2026-11-04 07:30 UTC)

Purpose: prove that **no bar stored before the H1 fix** was frozen from partial data, and that the stored data contains no integrity defect.

**Method:**
- One full `candleSnapshot` per interval (startTime=0), with the request time recorded.
- Only bars that were closed (end + 5 s) at request time count as "closed at source".
- Every stored closed bar is compared field by field (open, high, low, close, volume, trade count, open/close time) as exact `Decimal`.
- Both databases are audited: the main `autotrader_dev` and the outage-test `autotrader_chaos`.

| DB / interval | Closed bars stored | Verified against mainnet | **Value differences** | Bad times / alignment | Closed in DB but open at source | Future bars | Closed before end+5 s | Duplicates | Gaps / unreported |
|---|---|---|---|---|---|---|---|---|---|
| dev 30m | 5,038 | **5,006** | **0** | 0 | 0 | 0 | 0 | 0 | 0 / 0 |
| dev 1h | 5,019 | **5,003** | **0** | 0 | 0 | 0 | 0 | 0 | 0 / 0 |
| chaos 30m | 5,000 | **4,976** | **0** | 0 | 0 | 0 | 0 | 0 | 0 / 0 |
| chaos 1h | 5,000 | **4,988** | **0** | 0 | 0 | 0 | 0 | 0 | 0 / 0 |

**Results:**
- **19,973 closed bars verified, 0 differences.**
- The new `autotrader repair-candles` dry run independently confirmed the dev result: 5,007 + 5,003 compared, 0 differences.
- **No correction was needed or applied.**

**Not verifiable now:**
- 48 bars in dev (32 × 30m from 2026-07-22 08:00, 16 × 1h from 2026-04-09 04:00) and 36 in chaos are older than the API's current window.
- H1 can only affect a bar closed by a request sent within seconds of the bar's end. Every bar closed that way (2026-11-03 15:43 → 2026-11-04 07:20) is recent and inside the verified range.
- The unverifiable bars were stored by the first backfill, months after they closed, and 8 + 4 of them were also verified on 2026-11-03 16:02.

**Forming rows:**
- dev 30m showed 2 forming rows at 07:30:07 UTC: the 07:00 bar (not yet finalized; finalize runs within ~35 s) and the new 07:30 bar from the WS. Neither is marked closed.
- chaos holds one leftover forming row per interval from the stopped test collector. Since the M3 fix the API never presents it as live (`stale_forming_open_time`).

### Controlled correction path (migration 0005)
Migration 0004 made closed bars immutable for **everyone, including the owner**, so a corrupt bar could not have been repaired at all. Migration 0005 keeps the default protection and adds one audited path:
- **Default:** any UPDATE of a closed bar is rejected, as before.
- **Allowed only if all of these hold:**
  - a correction ticket is set for the transaction;
  - the session user is **not** a member of `autotrader_writer` / `autotrader_reader` (the collector and the API cannot use it, even with a ticket);
  - the bar's identity is unchanged and it stays closed.
- The trigger itself writes every correction (before/after, ticket, user) into the append-only `audit_log`.
- **`autotrader repair-candles`:**
  - dry run by default (read-only);
  - `--apply` requires `--ticket` and `--backup-dir`;
  - writes a JSON backup before any change;
  - corrects all rows in one transaction under the single-writer lock;
  - records `REVISED_CLOSED_CANDLE` with the ticket.
- **Verified:**
  - 6 PostgreSQL tests: ticket required; service role refused even with a ticket; audit row written; the ticket is transaction-local; identity changes and reopening blocked; full backup → apply → idempotent flow.
  - Live: `--apply` without a ticket → `TICKET_AND_BACKUP_REQUIRED`; while the collector runs → `COLLECTOR_RUNNING`.
- **Found while doing this:** Alembic's `version_num` is VARCHAR(32), and the first revision id was 33 characters. It was renamed, and a static test now guards all revision ids.

## 8. Known limitations
1. **History depth** is limited by the API (~5,000 bars per interval: 30m ≈ 104 days, 1h ≈ 208 days). That's enough for development and validation, but **thin for a statistically strong backtest**, especially at 1h. Phase 2 must report sample sizes and label results INCONCLUSIVE when appropriate. Coverage grows as long as the collector keeps running.
2. **Closed-bar latency:** a bar becomes "closed" in the DB about 5–35 s after the period ends (grace + 30 s finalize cycle).
3. **The reconnect delay** after a long outage can reach the backoff cap (observed 38 s, max 60 s).
4. **(Resolved)** Bars closing during an outage: verified live by the ~14.5-hour outage on 2026-11-04 (§4.2). Note that while the collector is down **no new data is collected**: the gap is filled on restart only because the API still serves those bars. Outages longer than the API window (~104 days at 30m) would lose data permanently, so production needs a restart policy and alerting.
5. **External WTI session calendar** (blueprint §2.5) is not implemented yet. It is needed before any paper entries (Phase 3). Phase 1 only collects data.
6. **No authentication** on the web or API yet (read-only, localhost-bound). Required before any online exposure (Phase 4).
7. **The advisory lock** prevents two writers per database (collector or CLI backfill). Running collectors against *different* databases is allowed by design (the isolated chaos test relied on this). Any role with DB access can take the lock key and block the collector from starting (it cannot write anything).
8. **`hyperliquid.gitbook.io` is blocked** from this environment. The fee and tick-size notes (ADR-004/005) are still not re-verified against the docs.
9. **REST rate limits:** heavy full-history requests from a shared egress can hit HTTP 429. The collector only makes small incremental requests, but large manual backfills should be spaced out.

## 9. Recommendation

**Phase 1 is ready to merge, in this order:**
1. PR #1 (Phase 0, includes all QA fixes up to `9177cd1`) into `main`.
2. Retarget PR #2 to `main`, re-run CI, then merge.

The data engine meets the Phase 1 gate:
- Restart and disconnect/reconnect recover without loss or duplicates (verified live and in tests).
- All observations carry provenance.
- Stored data matches the source exactly.
- PAPER-only guarantees hold.

It is a sound basis for the Phase 2 backtesting engine, which must read only via `closed_candles(as_of=…)`.
