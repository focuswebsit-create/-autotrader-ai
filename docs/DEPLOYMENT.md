# Deployment Plan — 24/7 Forward Testing (PAPER-ONLY)

**Status:** plan + ready-to-use artifacts. **Nothing is deployed.**
**Scope:** PostgreSQL, market-data collector, forward-test worker, API and Hebrew dashboard, running unattended for 30–60+ days.
No wallet, no keys, no orders: the only outbound traffic is the public Hyperliquid Info API/WebSocket (read-only, allow-listed in code) and, optionally, a monitoring ping and an off-site backup upload.

> **סיכום בעברית:**
> - **ההמלצה:** שרת וירטואלי יחיד ב־Hetzner Cloud (2–4 vCPU, 4–8GB RAM), עם Docker Compose, גיבוי לילי מאומת שמועתק גם מחוץ לשרת, וגישה פרטית דרך Tailscale בלי שום פורט ציבורי.
> - **ניטור:** שירות חיצוני מסוג "dead-man's switch" (למשל healthchecks.io) שולח התראה כשהשרת או אחד הרכיבים מפסיקים לפעול.
> - **עלות משוערת:** כ־€6–12 לחודש. יש לאמת את המחיר באתר הרשמי.
> - **כלל חשוב:** אין לעדכן קוד בזמן בדיקת Forward פעילה. עדכון כזה עוצר את הבדיקה (INTEGRITY_HALT), כפי שתוכנן.

## 1. Target architecture

```
                 (no public ports)            Tailscale (private, device-authenticated)
 owner's phone/PC ─────────────────────────────► server:3000 (web)  ─► api:8000 (GET only)
                                                                         │
 Hyperliquid mainnet (public Info API + WS, read-only) ◄── collector ──► PostgreSQL 16 ◄── forward worker
                                                                         │        (volume on local SSD)
                                       backup (nightly pg_dump + verify) ┘
                                       monitor ──► healthchecks.io (dead-man's switch: alerts by e-mail/Telegram)
```

| Requirement | How it is met | Artifact |
|---|---|---|
| PostgreSQL with persistent storage | Named volume `pgdata` on the server SSD. Least-privilege users: the API reads only; the workers can't DELETE, DDL or TRUNCATE. | `docker-compose.yml`, `infra/db/*` |
| Backups | Nightly `pg_dump` (custom format), checked with `pg_restore --list`, with a sha256 and 30-day retention. Off-site copy (§5). Monthly restore drill. Provider snapshots as a second layer. | `infra/deploy/backup.sh`, `restore-drill.sh` |
| Continuous collector | `collector` service: WS stream, reconnect with backoff, REST gap fill, single-writer lock. | existing (Phase 1) |
| Continuous forward worker | `forward` service: records decisions as bars close; funding sync every 10 min. | `forward/worker.py` |
| API + Hebrew dashboard | `api` (GET only) and `web` (Next.js), bound to 127.0.0.1 and reached via Tailscale. | existing |
| Restart after a crash | `restart: always` on every service. Docker starts on boot. Recording is idempotent: after a restart the worker catches up from the closed bars (`late=true`) and can't duplicate them (unique key). | `docker-compose.prod.yml` |
| Health monitoring and alerts | `/api/forward/health` returns 503 with alert codes when the DB, collector, WS, data freshness, forward worker or recording fails, or a test is halted. The `monitor` service pings an external dead-man's switch every 5 min; silence or `/fail` triggers an alert. | `api/forward.py`, `infra/deploy/monitor.sh` |
| Decision and event logs | Append-only DB tables (`forward_decisions`, `forward_events`, `system_events`, `ingestion_runs`), immutable by trigger. Container logs rotated at 20 MB × 10. | migrations 0003–0008 |
| Protection against data loss | Immutable tables; nightly verified dumps on the server and off-site; restore drill; provider backups. RPO ≤ 24 h. Optional WAL archiving (§5) lowers it to minutes. Lost candles can be re-fetched from Hyperliquid while they are within the last 5,000 bars (~104 days at 30m). | |
| Access security | No public ports (everything binds to 127.0.0.1); Tailscale for the owner's devices; optional Caddy with HTTPS + basic auth + GET-only filter. DB reachable only on the host. Firewall: deny all inbound except SSH (key-only) — or SSH via Tailscale only. | `infra/deploy/Caddyfile.example` |

## 2. Hosting options (prices are indicative — verify on the official pages before ordering)

Resource need (measured locally):
- **RAM:** under ~1.5 GB at runtime. Building the web image needs ~2 GB.
- **Disk:** data grows by less than 1 GB per year (the full DB backup today is 0.9 MB).
- **CPU:** a forward tick re-prepares indicators over the history, which takes well under a second today and grows linearly.

| Option | Monthly cost (approx.) | Pros | Cons / limits |
|---|---|---|---|
| **A. Hetzner Cloud VPS** (CX23 2 vCPU/4 GB, or CX33/CPX32-class 4 vCPU/8 GB) + Hetzner backups (+20%) + off-site storage | **≈ €5–12** (2026 prices rose ~30%; CX23 ≈ €4, CX32-class ≈ €8.5, +20% backups) | Cheapest; full control (our DB triggers, roles, Docker Compose as tested); EU data centres; snapshots | You run the OS updates; single machine (no HA); EU latency to the Hyperliquid API is fine for 30m bars |
| **B. DigitalOcean** Droplet 4 GB + Managed PostgreSQL | **≈ $40–90** (Droplet ≈ $24; managed PG $15 for 1 GB … ≈ $61 for 4 GB, backups extra) | Managed DB with automatic backups and PITR; good docs | 4–8× the cost; managed PG can limit `CREATE ROLE` and superuser features (migrations create NOLOGIN roles and triggers, so this must be tested); two services to secure |
| **C. AWS Lightsail** 2–4 GB instance (+ optional managed DB) | **≈ $10–20** VM; managed DB from ≈ $15 | Predictable bundles; AWS ecosystem; snapshots | Small bundles throttle CPU; static-IP and egress rules; managed DB has the same role caveats as B |
| **D. PaaS (Render / Railway / Fly.io)** | **≈ $35–65** for always-on worker + collector + web + Postgres | Git-push deploys; managed restarts and TLS | Always-on workers cost extra; managed Postgres restrictions; the 3 long-running processes and our compose setup must be split up; less control over clock, triggers and roles; usage-based bills (Railway, Fly) are harder to predict |
| **E. Home server / Raspberry Pi** | ≈ €0 (electricity) | Free; data at home | Power, ISP and router outages break the forward test (catch-up only within the API's 5,000-bar window); no SLA; not recommended for a 60-day unattended test |

Supporting services (optional, small):
- **Off-site backups:**
  - Backblaze B2: about $6/TB-month, so effectively cents for this data.
  - Hetzner Storage Box: from about €3–4/month.
- **Monitoring:**
  - healthchecks.io: free tier, about 20 checks, with e-mail/Telegram alerts.
  - UptimeRobot / Better Stack: free tiers.
- **Private access:**
  - Tailscale Personal: free for a few users and devices.
  - Cloudflare Tunnel + Access: free for small teams.

Sources (third-party summaries; official pages are authoritative):
- Hetzner 2026: [bitdoze](https://www.bitdoze.com/es/hetzner-cloud-precios-2026/), [betterstack](https://betterstack.com/community/guides/web-servers/hetzner-cloud-review/), [hetzner.com](https://www.hetzner.com/cloud/regular-performance)
- DigitalOcean: [infratally](https://infratally.com/articles/digitalocean-droplet-pricing-guide-2026/), [kuberns](https://kuberns.com/blogs/digitalocean-pricing/)
- PaaS: [devtoolpicks](https://devtoolpicks.com/blog/railway-vs-render-vs-fly-io-solo-developers-2026), [techsy](https://techsy.io/en/blog/railway-vs-render-vs-fly-io)
- Lightsail: [cloudburn](https://cloudburn.io/blog/amazon-lightsail-pricing), [kuberns](https://kuberns.com/blogs/aws-lightsail-pricing-your-comprehensive-guide/)

### Recommendation
Choose **Option A: a Hetzner Cloud VPS with 2–4 vCPU and 4–8 GB RAM in an EU location**, with these add-ons:
- Hetzner automatic backups;
- a nightly verified `pg_dump` copied off-site;
- Tailscale for access, with no public ports;
- healthchecks.io as the dead-man's switch.

It runs exactly the Docker Compose stack that CI already builds and tests, keeps our database-level integrity guarantees (triggers, roles, wall-clock check), and costs roughly €6–12 per month. The trade-off is a single machine. For a 30–60 day paper test that is acceptable: a short outage is recorded as late, catch-up decisions, and longer ones as DATA_GAP/DATA_STALE events, never hidden.

## 3. Rollout runbook (for when you approve deployment)

1. **Server:**
   - Create the VPS (Ubuntu 24.04 LTS), enable provider backups, and add your SSH public key (no password login).
   - Install Docker Engine + Compose, Tailscale and `unattended-upgrades`.
   - Firewall: deny all inbound; allow SSH only, ideally only via Tailscale.
2. **Code:** `git clone` the repository at the merged `main` commit, then `cp .env.example .env` and set strong random values for `POSTGRES_PASSWORD`, `AUTOTRADER_API_DB_PASSWORD`, `AUTOTRADER_WORKER_DB_PASSWORD` and `HEALTHCHECK_PING_URL`. Also set `AUTOTRADER_GIT_SHA=$(git rev-parse HEAD)`. **Never** add any key or wallet variable: the app refuses to start if one exists.
3. **Start:** `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
4. **Verify:**
   - `docker compose ps`: all services healthy;
   - `autotrader safety-check`;
   - `/api/collector/status` and `/api/forward/health`;
   - the dashboard through the Tailscale IP.
5. **Data:** the collector backfills automatically. Then run `docker compose run --rm forward autotrader backtest` once, which stores the reference backtest with the final code.
6. **Freeze (once):** `docker compose run --rm forward autotrader forward freeze`. Tests start at the next 30m boundary.
7. **Monitoring:**
   - Create a healthchecks.io check with a 5-minute period and 15-minute grace, with e-mail/Telegram alerts.
   - Confirm that the first pings arrive.
   - Test an alert by stopping `forward` for 20 minutes.
8. **Backups:**
   - Configure the off-site copy (§5).
   - Run `infra/deploy/restore-drill.sh` once after the first nightly backup, then monthly.
9. **Observation:** leave the stack running untouched for 30–60+ days. Check `/forward` weekly; alerts cover failures.

## 4. Operating rules during an active forward test

- **Do not change code that the test depends on.** The covered files are hashed, and any change halts the test with `INTEGRITY_HALT`. That protection is intended.
- **OS, Docker and PostgreSQL patch updates are fine.** Container restarts only cause late catch-up decisions.
- **Python dependencies are pinned** (`uv.lock`) and the images are rebuilt only on purpose. Don't rebuild during a test unless a security fix requires it. If you must, stop the test, deploy, and freeze a new one; both tests stay visible.
- **Stopping a test** (`autotrader forward stop --id N --reason "..."`) is final. A stopped test can never PASS; a new freeze is a new test.
- **Disk usage:** the alert threshold is 80%. The DB grows by well under 1 GB per year.

## 5. Data-loss protection details

| Layer | RPO | Notes |
|---|---|---|
| Postgres on SSD + `restart: always` | — | Survives process and container crashes. |
| Nightly `pg_dump` (verified, sha256, 30 days) in `./backups` | 24 h | `backup` service. |
| Off-site copy of `./backups` | 24 h | Host cron, e.g. `rclone copy ./backups b2:autotrader-backups --max-age 48h`. Use a write-only key scoped to one bucket, stored only on the server. |
| Provider server backups (Hetzner: daily, 7 kept) | 24 h | Protects against loss of the whole VM. |
| Optional: WAL archiving (`wal-g` / `pgBackRest`) to object storage | minutes | Only worth adding if you need sub-day RPO. |
| Candles re-fetchable from Hyperliquid | — | Only the most recent 5,000 bars per interval; the forward journal itself is not re-creatable, so its backups matter most. |

## 6. Security checklist (final review, 2026-11-04)

- [x] PAPER-ONLY:
  - [x] HTTP and WS allow-list; `/exchange` blocked.
  - [x] Startup refuses wallet- or key-like environment variables.
  - [x] No signing libraries.
  - [x] `safety-check` passes.
- [x] Every API route is GET; POST/PUT/PATCH/DELETE return 405. This is now verified for every route, including those added via routers (a test gap in the earlier check was found and fixed).
- [x] The web proxy forwards only allow-listed GET paths.
- [x] Least-privilege DB users: the API has SELECT only; workers have INSERT, plus UPDATE on the market-data tables only, and no DELETE, TRUNCATE or DDL.
- [x] Forward journal: UPDATE, DELETE and TRUNCATE are rejected; writes must happen in real time (±5 min of the DB clock), which also rejects replays.
- [x] Dependency audits: `npm audit` reports 0 vulnerabilities; `pip-audit` finds no known vulnerabilities in the locked Python dependencies.
- [ ] **Before any exposure beyond Tailscale:** put the dashboard behind authentication (Caddy basic auth or Cloudflare Access). The app itself has no login yet; Phase 4 will add role-based access.
- [ ] Rotate the DB passwords from `.env.example`. Never commit `.env`.

## 7. What the owner needs to do (no payment details or keys are needed by me)

1. Choose the provider (recommended: Hetzner Cloud) and create an account yourself.
2. Create the VPS and register your own SSH public key. Give me nothing secret; if you want me to run the rollout, I only need the commands' output or SSH access on a machine you control.
3. Create free accounts for Tailscale and healthchecks.io, plus optionally Backblaze B2. You keep all credentials; they go only into `.env` on the server.
4. Merge the Phase 2.5 PR, then follow §3.
