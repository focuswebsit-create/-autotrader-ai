# Phase 3 Plan — Autonomous Paper Trading & Live Trading Terminal

**Status:** plan only. Development starts after the Phase 2.5 follow-up (PR #5) is merged and the owner approves.
**Non-negotiable:**
- PAPER-ONLY: no wallet, no keys, no signing, no order to any exchange.
- The existing read-only API stays unchanged.
- The frozen strategies are used as they are.
- The terminal is OFF by default and carries a permanent "strategies are experimental, no proven edge" warning.

> **סיכום בעברית:**
> - **מה נבנה:** מנוע מסחר וירטואלי עצמאי, עם Ledger חשבונאי שאפשר לשחזר ומגבלות סיכון שנאכפות בשרת ובמסד הנתונים.
> - **שכבת בקרה:** Control API מאובטח ונפרד, עם התחברות, הרשאות מנהל, CSRF, Rate Limit ו־Audit.
> - **המסך:** `/terminal` בעיצוב Dark Premium בעברית, עם גרף חי, כפתורי "התחל לסחור לבד" ו"עצור", ורווח/הפסד כולל ויומי בגדול.
> - **מקור הנתונים:** כל מספר מחושב מה־Ledger ומנתוני שוק אמיתיים. אין נתונים מומצאים.

## 0. Constraints carried over from Phase 2.5

- **Frozen code.** Forward tests hash the decision, fill and evaluation code (`forward/freeze.py:CODE_FILES`). Phase 3 must **not modify** those files: it imports them read-only and adds new modules. If a shared change ever becomes unavoidable, the running forward tests are halted (marked `INTEGRITY_HALT`, never deleted) and re-frozen after the merge.
- **Separate engines.** Phase 3 adds a new `paper/` package and new tables. Forward tests and backtests are untouched, and their history is kept.

## 1. Architecture

```
Hyperliquid mainnet (public, read-only)
   │ WS: candle 30m/1h, activeAssetCtx (mark/oracle/funding), l2Book (bid/ask/depth)
   ▼
collector (existing) ──► PostgreSQL ◄── paper-trader worker (NEW, single instance, advisory lock)
                              ▲              │ decisions from frozen strategies on closed bars
                              │              │ fills simulated from the live L2 book (conservative)
read-only API (GET, existing) │              ▼
control API (NEW, auth+CSRF) ─┘        paper_* tables (append-only journal + ledger)
   ▲
web /terminal (NEW): login → dashboard → start/stop/kill (admin only)
```

## 2. Data model (new migration; append-only, with triggers like 0006–0008)

| Table | Purpose |
|---|---|
| `paper_accounts` | One virtual account: 1,000 USDC initial, currency, created_at, frozen risk config + hash. |
| `paper_bot_state_events` | State-machine transitions: STOPPED → STARTING → RUNNING ↔ PAUSED; any state → DEGRADED or HALTED. Each row records the actor and reason. The current state is the latest row. |
| `paper_decisions` | One row per closed 30m bar while RUNNING: decision, reason, rationale, market snapshot (bid/ask/depth/mark/oracle/funding), strategy hash, and a unique `decision_id = hash(account, strategy, bar)`. |
| `paper_orders` | Order state machine: NEW → (FILLED / PARTIALLY_FILLED / REJECTED / EXPIRED). `client_order_id` is unique, so a restart can't double-submit. Simulated only. |
| `paper_fills` | Price, size, fee, spread, slippage and L2 snapshot reference per fill. |
| `paper_positions` | Open/closed position, entry/exit, stop and target, close reason. |
| `paper_ledger` | Double-entry USDC movements: fee, realized P&L, funding, adjustments (none expected). Cash balance = Σ ledger. |
| `paper_risk_events` | Every block or halt with its reason: daily loss, drawdown, consecutive losses, stale data, missing funding, inconsistent account, strategy invalid, kill switch. |
| `users`, `sessions`, `audit_log` (exists) | Login, admin role, hashed passwords (argon2), session tokens; every control action audited. |

**Enforced in the database, not only in code:**
- The ledger is immutable.
- Reconciliation constraint: no INSERT into `paper_orders` while an unresolved `HALTED` or kill-switch event exists. A trigger checks the latest bot state.
- At most one open position per account (partial unique index).
- Entries per UTC day are capped (trigger).

## 3. Paper execution engine (`paper/`)

- **Signals:** the frozen strategies (`build_strategy` + the same params hash) run on **closed** bars only. One decision per bar, idempotent by `decision_id`.
- **Entry:** at the first fill opportunity after the signal bar closes, priced from the **live L2 book**. A buy walks the asks and a sell walks the bids for the order size, plus a conservative slippage buffer and the observed taker fee (0.009%, with the fee scenarios kept).
  - No fill if the book is stale (> 10 s), crossed, the spread exceeds 15 bps, depth is insufficient, or the mark/oracle divergence is abnormal.
  - Partial fill if the depth within the limit is insufficient; the remainder is cancelled.
- **Exits:** stop (stop-market against the live book) and target (limit, needs a trade through it), monitored from the WS book/trades. Funding is charged hourly from recorded rates; a missing rate blocks entries (it doesn't silently assume zero).
- **Sizing and risk** reuse the Phase 2 rules:
  - 0.25% risk per trade, 1% daily loss, 5% drawdown halt, 2× effective leverage;
  - one position, 4 entries per day, a halt after 3 consecutive losses (manual resume);
  - no entry on stale, missing or inconsistent data.
- **Kill switch:** immediately stops new entries (DB-enforced). "Close virtual positions and stop" closes at the live book with costs, after a confirmation dialog.
- **Crash safety:**
  - a single worker (advisory lock);
  - every action is an idempotent transaction keyed by `decision_id` / `client_order_id`;
  - after a restart the worker reconciles the open position, the orders and the ledger before it may trade (state STARTING); any mismatch leads to HALTED.
- **Ledger checks:**
  - cash = 1,000 + Σ realized − Σ fees − Σ funding;
  - equity = cash + unrealized at mark (margin isn't double-counted);
  - verified on every tick and in tests.

## 4. Control API (separate router/service, never on the read-only API)

- **Endpoints** (`POST /control/*`): `login`, `logout`, `bot/start`, `bot/stop`, `bot/pause`, `bot/kill`, `bot/close-and-stop`.
- **Security:**
  - session cookie (HttpOnly, Secure, SameSite=Strict) plus a CSRF token;
  - admin role required for every action;
  - rate limiting: 5 actions per minute per user, with login backoff;
  - every action written to `audit_log` with actor, IP hash, before/after state;
  - start is refused while another worker holds the lock or the bot isn't STOPPED (no double start).
- **Start sequence (server-side; any failure → DEGRADED with the reason shown):**
  1. DB healthy;
  2. collector RUNNING and the WS connected;
  3. 30m data fresh;
  4. L2 book fresh;
  5. strategy hash valid;
  6. risk config present;
  7. no other active engine;
  8. ledger reconciles.
  Only then RUNNING, and the UI shows the PAPER confirmation.
- The read-only API's GET-only test stays. A new test asserts that write routes exist only under `/control` and require auth + CSRF.

## 5. `/terminal` (Next.js, Dark Premium, Hebrew RTL, desktop + iPhone)

- **Top bar:** bot state badge; the big **▶ התחל לסחור לבד** / **■ עצור מסחר אוטומטי** toggle; a kill switch; a permanent warning "אסטרטגיות בניסוי — אין יתרון מוכח · PAPER בלבד".
- **P&L cards** (from the ledger only):
  - account balance and equity;
  - total P&L (USDC and %, green or red);
  - today's P&L, realized vs unrealized (UTC day; display time zone configurable);
  - closed-trade net P&L;
  - open-position unrealized P&L at live mark, clearly labelled "לא ממומש".
- **Chart:**
  - live xyz:CL candles (30m/1h);
  - EMA 20/50, RSI, ATR and volume;
  - LONG/SHORT entry and exit markers;
  - stop and target lines, plus an open-position band;
  - mark/oracle/last price and connection status;
  - a "stale" banner when the data isn't current.
- **"מה הבוט עושה עכשיו?"**, built from stored decisions and risk events only:
  - the active strategy and the market direction;
  - the checked conditions with their values;
  - whether a signal exists, and why a trade was or wasn't opened;
  - which risk limit blocked an entry;
  - the open position's status and the last decision time.
- **Below the chart:**
  - open position;
  - trade table: time, side, strategy, entry/exit, size, SL/TP, costs, net P&L, open and close reason, status; filters for day, week, month and all;
  - equity curve and daily P&L bars;
  - max drawdown, win rate, profit factor, trade counts.
- **Live updates:** server-sent events from a read-only stream endpoint, with automatic reconnection and a polling fallback.
- **Mobile layout:** P&L first, a sticky start/stop control, then the position, chart, trades and alerts.

## 6. Tests (all required before the PR)

- **Trades:** LONG and SHORT open/close; stop and target; ambiguous bar.
- **Restarts and outages:** a crash mid-trade (between order and fill, and between fill and ledger); a restart with an open position; a Hyperliquid WS disconnect; a stale book.
- **Integrity:** duplicate start and duplicate decisions; price gaps; missing funding; a sudden price jump (gap through the stop).
- **Accounting and risk:** P&L and ledger reconciliation (property tests); daily-loss halt; drawdown halt; consecutive losses; kill switch; close-and-stop.
- **Control:** start/stop through the UI with E2E Playwright; unauthorised and CSRF-less requests refused; rate limit; audit rows.
- **PAPER-ONLY:** no route or code path reaches `/exchange`, a signing function or a wallet field (static + runtime tests).
- **Mobile:** iPhone viewport screenshots and an overflow check.
- **Regression:** the Phase 0–2.5 test suites and the forward-test code hash unchanged.

## 7. Delivery steps (each committed and tested before the next)

1. Branch from `main` after PR #5 is merged; migration + models + ledger invariants (tests first).
2. Paper engine core: decisions, sizing, L2 fill model, exits, funding, risk, crash recovery.
3. Auth and the Control API: users, sessions, CSRF, rate limit, audit, state machine.
4. Read-only terminal endpoints and the SSE stream.
5. `/terminal` UI, desktop and mobile.
6. Compose service `paper-trader`, health integration (`/api/forward/health` → pipeline health), deployment doc update.
7. Independent code and security review; fixes; CI green; PR to `main` (not merged by me).

## 8. Out of scope

Any real execution path, wallet or API key; strategy changes or optimisation; claims of profitability.
