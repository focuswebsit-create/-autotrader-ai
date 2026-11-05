# Phase 3 Report — Autonomous PAPER Trading Engine & Live Terminal

**Mode:** PAPER ONLY: virtual money, no wallet, no keys, no real orders · **Market:** Hyperliquid HIP-3 `xyz:CL` (WTI) · **Date:** 2026-11-04

> ⚠️ **Experimental and virtual.** Trading here is simulated with 1,000 virtual USDC. No strategy in this project has shown a statistically proven edge (Phase 2 / 2.5 verdicts: INCONCLUSIVE). Nothing in this report or in the terminal claims the bot is profitable.

## 1. What was built

| Component | Where | What it does |
|---|---|---|
| PAPER execution engine | `src/autotrader/paper/engine.py`, `book.py`, `config.py` | Runs the frozen strategy (`breakout_volume` v1.0.0) on each 30m bar that closes **after** the bot was started. It opens and closes LONG/SHORT positions automatically. Fills walk the live L2 book and include fees, slippage and funding. It applies SL/TP and the risk limits. |
| Paper worker | `src/autotrader/paper/worker.py`, `autotrader paper run` | A single instance (advisory lock). It fetches the public `l2Book` every 3 s and runs each account in one transaction. It syncs funding, recovers after a restart, and publishes a heartbeat and the pre-flight readiness. |
| Database enforcement | migration `0009_paper_trading` | 13 new tables, all append-only: no UPDATE, DELETE or TRUNCATE. Real-time stamps (±5 min of the DB clock). Entry guard: RUNNING only, one open position, daily entry cap. Least-privilege roles. |
| Read API | `src/autotrader/api/paper.py` | GET only: `/api/paper/summary`, `trades`, `decisions`, `events`, `equity` and `health`. |
| Control API | `src/autotrader/control/` (separate service, port 8001) | Login, ADMIN role, CSRF, Origin check, rate limits, idempotency, audit log. It only **appends** commands; the worker executes them. |
| Terminal | `apps/web/app/terminal`, `apps/web/app/login` | Dark Premium, Hebrew RTL, desktop and iPhone. Controls, P&L, live chart, bot intelligence, trades, events, equity. |
| Deployment | `docker-compose*.yml`, `infra/db/roles.sql`, `infra/deploy/monitor.sh` | `paper-trader` and `control` services. Auto-restart. The health monitor covers the paper trader. |

Architecture decisions: ADR-032 (separate append-only engine), ADR-033 (conservative fills from the L2 book), ADR-034 (Control API). See `docs/DECISIONS.md`.

## 2. Simulation rules (all conservative)

- **Entry:**
  - Only on a bar that closed after START/RESUME, and within 180 s of the close. A missed window is recorded as `ENTRY_WINDOW_MISSED` and never traded later.
  - A taker fill walks the asks (LONG) or the bids (SHORT), plus 2 bps extra slippage, rounded against us.
  - It is refused if it would move more than 0.5% through the book, if the spread is over 15 bps, or if the book is older than 15 s.
- **Size:**
  - Risk is 0.25% of equity: the loss to the stop plus a 10 bps stop-slippage estimate plus fees, checked against the simulated fill. Leverage is at most 2×.
  - Minimum notional is $10, and the size uses the market's `szDecimals`.
- **Exits:**
  - A stop is a market walk of the book.
  - A target is a limit that fills only when the book trades through it with enough depth.
  - Anything beyond the visible 20 levels fills at the worst level.
  - Each fill pays the observed taker fee of 0.009% (ADR-028).
- **Funding:**
  - Every hour, the recorded rate × notional at that hour's close; a positive rate means longs pay.
  - If the rate is missing for more than 30 min, an assumed adverse rate is charged and a `FUNDING_ASSUMED` risk event is recorded.
- **Risk limits:**
  - A 1% daily loss blocks entries for the UTC day.
  - A 5% drawdown, measured from the last owner reset, or 3 losses in a row → **HALTED**.
  - At most 4 entries per day, and one position at a time.
  - No entry when inputs are stale or abnormal: collector, candles, book, funding, bar range or ledger reconciliation.
- **States:**
  - STOPPED, STARTING, RUNNING, PAUSED, DEGRADED, HALTED.
  - In DEGRADED, after missing data, no new entries are taken; an open position stays managed, and the bot recovers automatically.
  - The Kill Switch sets HALTED: no new entries, and an open position keeps its stop and target. Close-and-stop exits at the live book.
- **Accounting:** the balance is always the sum of the ledger in exact `Decimal`. P&L, equity, win rate, profit factor and drawdown are computed from the paper ledger and positions, **never from backtests**.

## 3. Start gating ("התחל לסחור לבד")

The button is enabled only when **all** of these hold:
1. The user is logged in with the ADMIN role.
2. The bot is STOPPED and no command is pending.
3. The worker's live pre-flight checks are fresh and all green:
   - database;
   - collector running and WS connected;
   - fresh candles;
   - fresh, uncrossed L2 book;
   - funding data;
   - strategy code hash unchanged;
   - risk limits loaded;
   - ledger reconciles;
   - single engine;
   - PAPER-only.
4. The user ticks an explicit "experimental and virtual" acknowledgement in the confirmation dialog.

When it runs START, the worker repeats every check. The UI view is only a preview.

## 4. Security

- **PAPER-ONLY cannot be bypassed:**
  - the only network path is the public Info API through the allow-listed `InfoClient`;
  - `/exchange` is blocked;
  - wallet- or key-like environment variables abort startup;
  - the account config refuses to load without `paper_only`.
- **Control:**
  - passwords: scrypt, minimum 12 characters; unknown users are checked against a dummy hash, so they cannot be enumerated; 5 failures in 15 min lock the user and the client;
  - session cookie: 32 random bytes, HttpOnly, SameSite=Strict, Secure in production; only its hash is stored; 12 h TTL; logout revokes it, and that is the only change the DB allows;
  - every change requires the HMAC CSRF header, an allowed Origin and the ADMIN role, within 6 actions per minute;
  - idempotency keys and a per-account advisory lock make double clicks and two tabs produce one command;
  - every action is written to `audit_log`.
- **Database:**
  - only the paper-trader's role (`autotrader_trader`) may write PAPER rows;
  - the collector, API and Control API cannot;
  - the Control API's role can only append commands and audit/auth rows, and revoke sessions;
  - the reader and writer roles cannot read the `auth_*` tables.
- **Independent review:** a separate reviewer audited the whole Phase 3 diff and found 1 medium engine defect, 2 medium auth weaknesses and 5 low issues. Everything is fixed, and each fix has a regression test (see §5).

## 5. Tests

All Phase 3 tests are automated and run in CI. The PostgreSQL tests run against a real PostgreSQL 16.

| Suite | Tests | Covers |
|---|---|---|
| `tests/unit/test_paper_engine.py` | 26 | Account and limits; pre-flight; LONG/SHORT fills from the book with costs; stop and target exits with net P&L; funding (recorded and assumed); no duplicates on re-tick; entry window (no retroactive trade); daily loss, max entries, 3 losses, drawdown halt **and reset**; restart recovery; Kill Switch; close-and-stop; late-committed commands; day-start equity; book parsing and walk; ledger-based summary. |
| `tests/unit/test_control_api.py` | 22 | Login and cookie flags; wrong password vs unknown user; lockout; origin; session expiry; logout revocation; CSRF missing, invalid or from another session; ADMIN vs VIEWER; idempotent double click; COMMAND_PENDING; state machine (start → pause → kill → reset); confirmations; rate limit; secret required and placeholder refused. |
| `tests/unit/test_paper_api.py` | 7 | GET-only routes; summary and trades from the ledger; stale book not marked; readiness from the worker; health for monitoring; bad inputs. |
| `tests/integration/test_paper_postgres.py` | 12 | Full round trip through the worker on NUMERIC accounting; DB refuses entries unless RUNNING and refuses a second open position; append-only and TRUNCATE; no backdated rows on any paper table; revoke-once sessions; role least privilege, including "the collector cannot forge paper rows"; concurrent commands from 4 tabs give 1 (the test fails without the lock); Control API → worker end to end with the real login roles. |
| `apps/web/e2e/terminal.e2e.cjs` | 12 checks | Live stack, desktop and iPhone 14: warning shown, start disabled for anonymous users, wrong password refused, admin login, explicit acknowledgement required, start → RUNNING (executed by the worker), pause, resume, no page errors, no horizontal overflow, touch targets ≥ 44 px. |

Whole repository: **506 passed, 4 skipped** (skipped = live-network tests), with PostgreSQL enabled. ruff, mypy (strict), eslint, tsc and `next build` are clean.

**The PostgreSQL tests found two real bugs that SQLite could not show; both are fixed:**
- the entry-guard trigger read a column that does not exist on `paper_positions` (PL/pgSQL does not short-circuit `AND`), so every real entry would have failed;
- the controller role could not `INSERT … RETURNING` into `audit_log`.

## 6. Live verification (real Hyperliquid mainnet data, read-only)

On this development container:
- **Services:** collector (WS connected), paper worker, read API, Control API and web, each with its own least-privilege DB login.
- **Readiness:** all 10 pre-flight checks were green on the live `xyz:CL` book (spread 0.8–2.5 bps).
- **Controls:** logging in and pressing "התחל לסחור לבד", pause and resume all went through the real worker, as did the Kill Switch path in the tests.
- **First live decision:** the 15:00 UTC bar, evaluated 33 s after it closed. Outcome NO_SIGNAL (`NO_SETUP`); no trade.
- **Reset:** after the review fixes the database was re-migrated and the account recreated. The bot has been RUNNING again since 15:39 UTC.
- **P&L:** at the time of writing the virtual account has **no closed trades and $0.00 P&L**. That says nothing about profitability either way.

Screenshots: `docs/screenshots/phase3-terminal-desktop.png`, `phase3-terminal-iphone.png`, `phase3-start-confirm.png`, `phase3-login-iphone.png`.

## 7. Limitations and what is not verified

- **Docker:** the images were not built here (no Docker daemon in this container). The compose files pass `docker compose config`; the CI `docker` job builds the images.
- **Continuous operation:** the live run lasted minutes in an ephemeral container. Meaningful paper results need weeks on the persistent server (docs/DEPLOYMENT.md); with ≤ 4 entries per day and a selective strategy, trades will be rare.
- **Fill model:** fills come from 20 visible levels at 3-second snapshots. Queue position, hidden liquidity and latency are not modelled; the extra slippage and worst-level rules are meant to keep the results pessimistic, but they remain a simulation.
- **Exposure:** read-only pages, including P&L, have no login; bot control does. Keep the site behind Tailscale or Cloudflare Access, and do not expose it to the open internet. The owner login is one password without 2FA.
- **Login lockout:** with `TRUST_PROXY_XFF=0` (the default) all clients share one address for the per-client lockout, so a stranger who can reach the login page can lock the owner out for 15 minutes. The private access layer above prevents this.
- **Upgrading an existing database volume:** re-run `infra/db/roles.sql` with `paper_password` and `control_password`; the init hook only runs on a new volume.

## 8. How to run

See README ("Run locally") and DEPLOYMENT.md §3, step 6: `autotrader paper init`, `autotrader paper run`, `autotrader user create --role ADMIN`, the control service, then open `/terminal`.
