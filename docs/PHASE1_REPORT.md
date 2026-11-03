# Phase 1 Report — Market Data Engine (xyz:CL, PAPER-ONLY)

**Date:** 2026-11-03 · **Branch:** `claude/phase1-market-data` · **PR:** #2 (stacked on PR #1 / Phase 0)
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

**Not exercised live:** a bar closing *during* the outage, since no bar boundary fell inside the 60-second window. That path is covered by the automated tests below (REST sync from the last closed bar after reconnect, in chunks).

### 4.2 Other live operational checks
- **Graceful restart** of the main collector, done twice: heartbeat `STOPPED`, then a restart with no duplicates (counts identical before and after).
- **Single writer:** a second `autotrader collect` against the same DB exited with **code 5** ("another collector is running").
- **REST rate limit:** heavy verification requests from this environment got HTTP 429. `InfoClient` retries 429 with backoff/`Retry-After`. The collector's own small requests were never rate-limited.

### 4.3 Automated tests (deterministic, run in CI)
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
- **pytest: 286 passed**:
  - 262 unit: safety 90, discovery 41, WS guard 29, candles 25, API 15 + market API 13, collector 11, resilience 9, storage 8, static 8, REST ingest 7, CLI 6;
  - 20 PostgreSQL;
  - 4 live mainnet (discovery, candles 30m/1h, guarded WS stream).
- **Web:** ESLint ✔, TypeScript ✔, `next build` ✔.
- **Browser checks:** desktop 1366 px and iPhone 14; no horizontal scroll, no console errors, CSP intact.
- **CI on GitHub:** backend ✔, frontend ✔, docker ✔. The final commit's CI result is reported in the PR.

## 8. Known limitations
1. **History depth** is limited by the API (~5,000 bars per interval: 30m ≈ 104 days, 1h ≈ 208 days). That's enough for development and validation, but **thin for a statistically strong backtest**, especially at 1h. Phase 2 must report sample sizes and label results INCONCLUSIVE when appropriate. Coverage grows as long as the collector keeps running.
2. **Closed-bar latency:** a bar becomes "closed" in the DB about 5–35 s after the period ends (grace + 30 s finalize cycle).
3. **The reconnect delay** after a long outage can reach the backoff cap (observed 38 s, max 60 s).
4. **No bar boundary fell inside the live outage window.** That path is covered by automated tests only.
5. **External WTI session calendar** (blueprint §2.5) is not implemented yet. It is needed before any paper entries (Phase 3). Phase 1 only collects data.
6. **No authentication** on the web or API yet (read-only, localhost-bound). Required before any online exposure (Phase 4).
7. **The advisory lock** prevents two collectors per database. Running collectors against *different* databases is allowed by design (the isolated chaos test relied on this).
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
