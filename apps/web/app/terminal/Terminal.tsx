"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useNow, usePoll } from "@/app/components/usePoll";
import { botCommand, getMe, logout, newKey, type BotAction, type Me } from "@/lib/control";
import { ageFrom, fmtAge, fmtNum, fmtUtc } from "@/lib/format";
import {
  blockHe,
  CHECK_LABEL,
  CLOSE_LABEL,
  controlError,
  money,
  pct,
  reasonHe,
  STATE_LABEL,
  tone,
  type BotEvent,
  type BotState,
  type Decision,
  type EquityCurve,
  type Summary,
  type Trade,
} from "@/lib/paper";
import EquityPanel from "./EquityPanel";
import TerminalChart from "./TerminalChart";

type Period = "day" | "week" | "month" | "all";
const PERIODS: { id: Period; he: string }[] = [
  { id: "day", he: "היום" },
  { id: "week", he: "שבוע" },
  { id: "month", he: "חודש" },
  { id: "all", he: "הכל" },
];

const ALLOWED: Record<BotAction, BotState[]> = {
  start: ["STOPPED"],
  resume: ["PAUSED"],
  pause: ["RUNNING", "DEGRADED"],
  stop: ["STARTING", "RUNNING", "PAUSED", "DEGRADED"],
  kill: ["STOPPED", "STARTING", "RUNNING", "PAUSED", "DEGRADED", "HALTED"],
  "close-and-stop": ["STOPPED", "STARTING", "RUNNING", "PAUSED", "DEGRADED", "HALTED"],
  "reset-halt": ["HALTED"],
};

const CONFIRM: Partial<Record<BotAction, { title: string; body: string; cta: string; danger?: boolean; needsConfirm?: boolean }>> = {
  start: {
    title: "התחלת מסחר וירטואלי אוטומטי",
    body: "הבוט יפתח ויסגור עסקאות LONG/SHORT וירטואליות לבד, על נתוני שוק חיים, בכסף מדומה בלבד (1,000 USDC). האסטרטגיות ניסיוניות ואין הוכחה שהן רווחיות. אין שום פקודה אמיתית.",
    cta: "התחל לסחור לבד",
  },
  kill: {
    title: "Kill Switch",
    body: "עוצר מיד כל כניסה חדשה ומעביר את הבוט למצב 'נעצר לבדיקה'. פוזיציה פתוחה (אם יש) נשארת עם הסטופ והיעד שלה. חידוש דורש איפוס ידני.",
    cta: "הפעל Kill Switch",
    danger: true,
  },
  "close-and-stop": {
    title: "סגירת הפוזיציה ועצירה",
    body: "הפוזיציה הווירטואלית הפתוחה תיסגר במחיר השוק הנוכחי בספר הפקודות (כולל עמלה והחלקה) והבוט ייעצר.",
    cta: "סגור ועצור",
    danger: true,
    needsConfirm: true,
  },
  "reset-halt": {
    title: "איפוס עצירה",
    body: "הבוט נעצר בגלל מגבלת סיכון או Kill Switch. איפוס מחזיר אותו למצב כבוי (לא מתחיל לסחור). ודא שבדקת את הסיבה.",
    cta: "אפס עצירה",
    needsConfirm: true,
  },
};

function Pnl({ value, className = "" }: { value: string | null | undefined; className?: string }) {
  const t = tone(value);
  return (
    <span dir="ltr" className={`tabular-nums ${t === "pos" ? "t-pos" : t === "neg" ? "t-neg" : ""} ${className}`}>
      {money(value)}
    </span>
  );
}

function Stat({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="t-stat">
      <div className="t-stat-label">{label}</div>
      <div className="t-stat-value">{children}</div>
      {hint ? <div className="t-stat-hint">{hint}</div> : null}
    </div>
  );
}

function StateBadge({ state }: { state: BotState | undefined }) {
  if (!state) return <span className="t-chip">—</span>;
  const s = STATE_LABEL[state];
  return (
    <span className={`t-chip t-chip-${s.tone}`} role="status" aria-label={`מצב הבוט: ${s.he}`}>
      <span className="t-dot" />
      {s.he}
    </span>
  );
}

function detailText(d: unknown): string {
  if (d == null) return "";
  if (typeof d === "string") return d;
  try {
    return JSON.stringify(d);
  } catch {
    return "";
  }
}

export default function Terminal() {
  const now = useNow(1000);
  const sum = usePoll<Summary>("paper/summary", 3000);
  const [period, setPeriod] = useState<Period>("all");
  const trades = usePoll<{ trades: Trade[] }>("paper/trades", 10_000, { period });
  const allTrades = usePoll<{ trades: Trade[] }>("paper/trades", 15_000, { period: "month" });
  const decisions = usePoll<{ decisions: Decision[] }>("paper/decisions", 10_000, { limit: "30" });
  const events = usePoll<{ events: BotEvent[] }>("paper/events", 10_000, { limit: "40" });
  const equity = usePoll<EquityCurve>("paper/equity", 60_000);

  const [me, setMe] = useState<Me | null>(null);
  const [meErr, setMeErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<BotAction | null>(null);
  const [pending, setPending] = useState<{ action: BotAction; from: BotState; at: number } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [dialog, setDialog] = useState<BotAction | null>(null);
  const [ack, setAck] = useState(false);
  const keys = useRef<Partial<Record<BotAction, string>>>({});

  useEffect(() => {
    let live = true;
    void getMe().then((r) => {
      if (!live) return;
      if (r.ok) setMe(r.data);
      else setMeErr(r.error);
    });
    return () => {
      live = false;
    };
  }, []);

  const s = sum.data;
  const state = s?.bot.state;
  const summaryAge = sum.at ? (now - sum.at) / 1000 : null;
  const online = !sum.error && summaryAge != null && summaryAge < 15;

  // a sent command is "pending" until the worker changes the state (or 30 s pass)
  useEffect(() => {
    if (!pending || !s) return;
    if (s.bot.state !== pending.from || now - pending.at > 30_000) {
      const t = window.setTimeout(() => setPending(null), 0);
      return () => window.clearTimeout(t);
    }
  }, [pending, s, now]);

  const run = useCallback(
    async (action: BotAction) => {
      if (!me || !state) return;
      const key = (keys.current[action] ??= newKey(action));
      setBusy(action);
      setMsg(null);
      const res = await botCommand(action, me.csrf_token, key, {
        reason: `terminal:${action}`,
        confirm: CONFIRM[action]?.needsConfirm ?? false,
      });
      setBusy(null);
      delete keys.current[action];
      if (res.ok) {
        setPending({ action, from: state, at: Date.now() });
        setMsg({ ok: true, text: "הפקודה נשלחה ומבוצעת על ידי מנוע ה־PAPER…" });
      } else {
        setMsg({ ok: false, text: controlError(res.error) });
        if (res.error === "NOT_AUTHENTICATED") setMe(null);
      }
    },
    [me, state],
  );

  const can = (a: BotAction) => !!state && ALLOWED[a].includes(state) && me?.role === "ADMIN" && !busy && !pending;

  const startBlockers = useMemo(() => {
    const out: string[] = [];
    if (!online) out.push("אין חיבור עדכני לשרת");
    if (!me) out.push("נדרשת התחברות");
    else if (me.role !== "ADMIN") out.push("נדרש משתמש מנהל (ADMIN)");
    if (state && state !== "STOPPED") out.push(`הבוט במצב "${STATE_LABEL[state].he}"`);
    if (s && !s.readiness.fresh) out.push("מנוע ה־PAPER לא מדווח (worker כבוי?)");
    else if (s && !s.readiness.ok) out.push("בדיקות המוכנות לא עברו");
    if (s && !s.account.risk) out.push("מגבלות הסיכון לא נטענו");
    if (pending) out.push("פקודה קודמת ממתינה");
    return out;
  }, [online, me, state, s, pending]);
  const startEnabled = startBlockers.length === 0 && !busy;

  const doLogout = async () => {
    if (me) await logout(me.csrf_token);
    setMe(null);
  };

  const err = sum.error;
  const pos = s?.position ?? null;
  const ld = s?.last_decision ?? null;
  const decAge = ld ? ageFrom(ld.evaluated_at, now) : null;
  const winRate = s?.stats.win_rate;
  const pf = s?.stats.profit_factor;
  const quote = s?.market.book ?? null;

  return (
    <div className="terminal">
      <header className="t-head">
        <div className="flex flex-wrap items-center gap-2">
          <span className="t-paper" aria-label="מצב מסחר וירטואלי">PAPER TRADING</span>
          <h1 className="text-base font-bold sm:text-lg">טרמינל מסחר וירטואלי</h1>
          <StateBadge state={state} />
          <span className={`t-chip ${online ? "t-chip-ok" : "t-chip-bad"}`}>
            <span className="t-dot" />
            {online ? "מחובר" : sum.at ? `מנותק · ${fmtAge(summaryAge)}` : "מתחבר…"}
          </span>
        </div>
        <nav className="flex flex-wrap items-center gap-3 text-xs">
          <Link href="/" className="t-link">נתונים</Link>
          <Link href="/forward" className="t-link">Forward</Link>
          <Link href="/backtest" className="t-link">Backtest</Link>
          {me ? (
            <span className="t-muted">
              {me.user} · {me.role}{" "}
              <button type="button" className="t-link" onClick={() => void doLogout()}>
                התנתק
              </button>
            </span>
          ) : (
            <Link href="/login?next=/terminal" className="t-link font-semibold">התחברות</Link>
          )}
        </nav>
      </header>

      <p className="t-warning" role="note">
        ⚠ מסחר ניסיוני ווירטואלי בלבד: כסף מדומה (1,000 USDC), ללא ארנק, ללא מפתחות וללא פקודות אמיתיות. אף אסטרטגיה לא
        הוכחה כרווחית; תוצאות עבר או סימולציה אינן מבטיחות דבר.
      </p>

      {err && (
        <p role="alert" className="t-alert t-alert-bad">
          {controlError(err)}
        </p>
      )}

      {/* ---------------------------------------------------------------- P&L */}
      <section className="t-pnl" aria-label="רווח והפסד וירטואלי">
        <div className="t-pnl-main">
          <div className="t-stat-label">רווח/הפסד כולל</div>
          <div className="t-pnl-big">
            <Pnl value={s?.pnl.total_pnl} />
          </div>
          <div className={`text-sm ${tone(s?.pnl.total_pct) === "neg" ? "t-neg" : tone(s?.pnl.total_pct) === "pos" ? "t-pos" : "t-muted"}`} dir="ltr">
            {pct(s?.pnl.total_pct)}
          </div>
          <div className="mt-2 text-xs t-muted">
            הון וירטואלי: <span dir="ltr" className="font-semibold text-[var(--t-fg)]">{money(s?.pnl.equity, false)}</span>
            {" · "}יתרה: <span dir="ltr">{money(s?.pnl.cash, false)}</span>
          </div>
          {s?.pnl.equity == null && s?.position ? (
            <div className="mt-1 text-xs t-warn">אין מחיר חי: הפוזיציה לא מתומחרת כרגע</div>
          ) : null}
        </div>
        <div className="t-stats">
          <Stat label="היום" hint="יום UTC">
            <Pnl value={s?.pnl.today_pnl} />
          </Stat>
          <Stat label="ממומש (נטו)">
            <Pnl value={s?.pnl.realized_net} />
          </Stat>
          <Stat label="לא ממומש">
            <Pnl value={s?.pnl.unrealized} />
          </Stat>
          <Stat label="Win Rate" hint={s ? `${s.stats.wins}/${s.stats.closed_trades} עסקאות` : undefined}>
            <span dir="ltr">{winRate == null ? "—" : `${(winRate * 100).toFixed(1)}%`}</span>
          </Stat>
          <Stat label="Profit Factor">
            <span dir="ltr">{pf == null ? "—" : pf === "inf" ? "∞" : pf.toFixed(2)}</span>
          </Stat>
          <Stat label="Drawdown מקס׳" hint={s ? `עצירה ב־${s.risk.drawdown_halt_pct}%` : undefined}>
            <span dir="ltr" className={Number(s?.stats.max_drawdown_pct) > 0 ? "t-neg" : ""}>
              {s ? `${Number(s.stats.max_drawdown_pct).toFixed(2)}%` : "—"}
            </span>
          </Stat>
          <Stat label="עמלות">
            <Pnl value={s ? String(-Number(s.pnl.fees)) : null} />
          </Stat>
          <Stat label="מימון (funding)">
            <Pnl value={s ? String(-Number(s.pnl.funding)) : null} />
          </Stat>
        </div>
      </section>

      <div className="t-grid">
        <div className="t-main">
          <TerminalChart
            trades={allTrades.data?.trades ?? []}
            position={pos}
            quote={quote}
            quoteFresh={!!s?.market.fresh}
            now={now}
          />

          {/* -------------------------------------------------------- trades */}
          <section className="t-card" aria-labelledby="t-trades">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 id="t-trades" className="text-sm font-semibold">עסקאות וירטואליות</h2>
              <div className="t-seg" role="group" aria-label="תקופה">
                {PERIODS.map((p) => (
                  <button key={p.id} type="button" aria-pressed={period === p.id} onClick={() => setPeriod(p.id)}>
                    {p.he}
                  </button>
                ))}
              </div>
            </div>
            {trades.data && trades.data.trades.length === 0 ? (
              <p className="text-sm t-muted">אין עסקאות בתקופה זו.</p>
            ) : (
              <div className="t-table-wrap">
                <table className="t-table">
                  <thead>
                    <tr>
                      <th>כיוון</th>
                      <th>פתיחה (UTC)</th>
                      <th>כניסה</th>
                      <th>יציאה</th>
                      <th>גודל</th>
                      <th>SL / TP</th>
                      <th>עמלות</th>
                      <th>נטו</th>
                      <th>סיבה</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(trades.data?.trades ?? []).map((t) => (
                      <tr key={t.id}>
                        <td>
                          <span className={t.side === "LONG" ? "t-pos" : "t-neg"}>{t.side}</span>
                          {t.status === "OPEN" ? <span className="t-chip t-chip-warn ms-1">פתוחה</span> : null}
                        </td>
                        <td dir="ltr">{fmtUtc(t.opened_at)}</td>
                        <td dir="ltr">{fmtNum(t.entry_price, 3)}</td>
                        <td dir="ltr">{t.exit_price ? fmtNum(t.exit_price, 3) : "—"}</td>
                        <td dir="ltr">{fmtNum(t.size, 3)}</td>
                        <td dir="ltr">
                          {fmtNum(t.stop, 3)} / {fmtNum(t.target, 3)}
                        </td>
                        <td dir="ltr">{t.fees ? `$${Number(t.fees).toFixed(4)}` : "—"}</td>
                        <td>{t.net_pnl ? <Pnl value={t.net_pnl} /> : <span className="t-muted">פתוחה</span>}</td>
                        <td className="t-wrap">
                          {t.close_reason ? CLOSE_LABEL[t.close_reason] ?? t.close_reason : t.open_reason ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <EquityPanel curve={equity.data} />
        </div>

        <aside className="t-side">
          {/* ------------------------------------------------------- controls */}
          <section className="t-card" aria-labelledby="t-ctl">
            <h2 id="t-ctl" className="mb-3 text-sm font-semibold">שליטה בבוט</h2>
            {state === "STOPPED" || !state ? (
              <>
                <button
                  type="button"
                  className="t-btn-start"
                  disabled={!startEnabled}
                  aria-describedby="start-why"
                  onClick={() => {
                    setAck(false);
                    setDialog("start");
                  }}
                >
                  {busy === "start" ? "שולח…" : "התחל לסחור לבד"}
                </button>
                <div id="start-why" className="mt-2 text-xs">
                  {startEnabled ? (
                    <span className="t-pos">כל הבדיקות עברו. המנוע יבדוק שוב בעת ההפעלה.</span>
                  ) : (
                    <ul className="space-y-0.5 t-muted">
                      {startBlockers.map((b) => (
                        <li key={b}>• {b}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            ) : (
              <div className="grid grid-cols-2 gap-2">
                {state === "PAUSED" ? (
                  <button type="button" className="t-btn" disabled={!can("resume")} onClick={() => void run("resume")}>
                    חידוש
                  </button>
                ) : (
                  <button type="button" className="t-btn" disabled={!can("pause")} onClick={() => void run("pause")}>
                    השהיה
                  </button>
                )}
                <button type="button" className="t-btn t-btn-stop" disabled={!can("stop")} onClick={() => void run("stop")}>
                  עצור מסחר
                </button>
                {state === "HALTED" && (
                  <button type="button" className="t-btn col-span-2" disabled={!can("reset-halt")} onClick={() => setDialog("reset-halt")}>
                    איפוס עצירה (לאחר בדיקה)
                  </button>
                )}
              </div>
            )}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <button type="button" className="t-btn-kill" disabled={!can("kill") || state === "HALTED"} onClick={() => setDialog("kill")}>
                Kill Switch
              </button>
              <button type="button" className="t-btn" disabled={!can("close-and-stop") || !pos} onClick={() => setDialog("close-and-stop")}>
                סגור פוזיציה ועצור
              </button>
            </div>
            {pending && (
              <p className="mt-2 text-xs t-warn" role="status">
                ממתין לביצוע הפקודה ({pending.action}) על ידי המנוע…
              </p>
            )}
            {msg && (
              <p className={`mt-2 text-xs ${msg.ok ? "t-pos" : "t-neg"}`} role="status">
                {msg.text}
              </p>
            )}
            {!me && meErr && meErr !== "NOT_AUTHENTICATED" && <p className="mt-2 text-xs t-neg">{controlError(meErr)}</p>}
            {s && (
              <p className="mt-3 text-[11px] t-muted">
                מצב: {reasonHe(s.bot.reason)} · מאז <bdi dir="ltr">{fmtUtc(s.bot.since, true)}</bdi>
              </p>
            )}
          </section>

          {/* ---------------------------------------------- bot intelligence */}
          <section className="t-card" aria-labelledby="t-intel">
            <h2 id="t-intel" className="mb-2 text-sm font-semibold">מה הבוט רואה</h2>
            <dl className="t-dl">
              <div>
                <dt>אסטרטגיה פעילה</dt>
                <dd>
                  <bdi dir="ltr">{s ? `${s.account.strategy_id} v${s.account.strategy_version}` : "—"}</bdi>
                  <span className="t-muted"> · מוקפאת</span>
                </dd>
              </div>
              <div>
                <dt>החלטה אחרונה</dt>
                <dd>
                  {ld ? (
                    <>
                      <bdi dir="ltr">{fmtUtc(ld.evaluated_at, true)}</bdi>
                      <span className="t-muted"> ({fmtAge(decAge)})</span>
                    </>
                  ) : (
                    "עדיין אין החלטות (רק על נרות שנסגרים אחרי ההפעלה)"
                  )}
                </dd>
              </div>
              {ld && (
                <>
                  <div>
                    <dt>נר שנבדק</dt>
                    <dd dir="ltr">{fmtUtc(ld.bar_open_time)} 30m</dd>
                  </div>
                  <div>
                    <dt>הזדמנות כניסה</dt>
                    <dd>
                      {ld.side ? (
                        <span className={ld.side === "LONG" ? "t-pos" : "t-neg"}>אות {ld.side}</span>
                      ) : (
                        <span className="t-muted">אין אות</span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>תוצאה</dt>
                    <dd>
                      {ld.outcome === "ORDER" && <span className="t-pos">נפתחה עסקה וירטואלית</span>}
                      {ld.outcome === "BLOCKED" && <span className="t-warn">נחסם: {blockHe(ld.block_reason)}</span>}
                      {ld.outcome === "NO_SIGNAL" && <span className="t-muted">אין עסקה ({ld.reason})</span>}
                      {ld.outcome === "IN_POSITION" && <span className="t-muted">כבר בפוזיציה</span>}
                    </dd>
                  </div>
                  {ld.rationale && (
                    <div>
                      <dt>נימוק</dt>
                      <dd className="t-wrap text-xs" dir="ltr">
                        {String((ld.rationale as { rule?: unknown }).rule ?? detailText(ld.rationale)).slice(0, 220)}
                      </dd>
                    </div>
                  )}
                </>
              )}
              <div>
                <dt>פוזיציה</dt>
                <dd>
                  {pos ? (
                    <span>
                      <span className={pos.side === "LONG" ? "t-pos" : "t-neg"}>{pos.side}</span>{" "}
                      <bdi dir="ltr">
                        {fmtNum(pos.size, 3)} @ {fmtNum(pos.entry_price, 3)}
                      </bdi>{" "}
                      · <Pnl value={pos.unrealized} />
                    </span>
                  ) : (
                    <span className="t-muted">אין פוזיציה פתוחה</span>
                  )}
                </dd>
              </div>
              {pos && (
                <div>
                  <dt>SL / TP</dt>
                  <dd dir="ltr">
                    <span className="t-neg">{fmtNum(pos.stop, 3)}</span> / <span className="t-pos">{fmtNum(pos.target, 3)}</span>
                    <span className="t-muted"> · סיכון ${Number(pos.planned_loss).toFixed(2)}</span>
                  </dd>
                </div>
              )}
              <div>
                <dt>מגבלות סיכון</dt>
                <dd className="text-xs">
                  {s ? (
                    <>
                      {s.risk.risk_per_trade_pct}% לעסקה · {s.risk.daily_loss_limit_pct}% יומי · {s.risk.drawdown_halt_pct}% DD
                      <br />
                      כניסות היום {s.risk.entries_today}/{s.risk.max_entries_per_day} · הפסדים ברצף {s.risk.consecutive_losses}/
                      {s.risk.halt_after_consecutive_losses}
                      {s.risk.daily_loss_limit_hit ? <span className="t-neg"> · מגבלה יומית הופעלה</span> : null}
                    </>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
              <div>
                <dt>ספר פקודות</dt>
                <dd className="text-xs">
                  {s?.market.fresh && quote ? (
                    <span dir="ltr">
                      spread {fmtNum(quote.spread_bps, 2)} bps · {fmtAge(ageFrom(quote.time, now))}
                    </span>
                  ) : (
                    <span className="t-neg">{s?.market.book_error ?? (s?.market.worker === "DOWN" ? "worker לא מגיב" : "לא זמין")}</span>
                  )}
                </dd>
              </div>
            </dl>
          </section>

          {/* ------------------------------------------------------ readiness */}
          <section className="t-card" aria-labelledby="t-ready">
            <h2 id="t-ready" className="mb-2 text-sm font-semibold">בדיקות מוכנות</h2>
            {s && s.readiness.checks.length > 0 ? (
              <ul className="space-y-1 text-xs">
                {s.readiness.checks.map((c) => (
                  <li key={c.check} className="flex items-start justify-between gap-2">
                    <span>{CHECK_LABEL[c.check] ?? c.check}</span>
                    <span className={c.ok ? "t-pos" : "t-neg"}>{c.ok ? "✓" : `✗ ${typeof c.detail === "string" ? blockHe(c.detail.split(",")[0]) : ""}`}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs t-neg">
                {s?.readiness.reason === "PAPER_WORKER_NOT_RUNNING" || s?.readiness.reason === "PAPER_WORKER_DOWN"
                  ? "מנוע ה־PAPER אינו פועל. לא ניתן להתחיל."
                  : "ממתין לדיווח מהמנוע…"}
              </p>
            )}
          </section>

          {/* --------------------------------------------------------- events */}
          <section className="t-card" aria-labelledby="t-ev">
            <h2 id="t-ev" className="mb-2 text-sm font-semibold">יומן אירועים</h2>
            <ol className="t-events">
              {(events.data?.events ?? []).slice(0, 25).map((e, i) => (
                <li key={`${e.time}-${i}`}>
                  <span className="t-muted" dir="ltr">
                    {fmtUtc(e.time, true).slice(5)}
                  </span>{" "}
                  <span className={e.type === "RISK" ? "t-warn" : ""}>{reasonHe(e.reason)}</span>
                  {e.type === "BOT" && <span className="t-muted"> → {STATE_LABEL[e.kind as BotState]?.he ?? e.kind}</span>}
                </li>
              ))}
            </ol>
          </section>

          <section className="t-card" aria-labelledby="t-dec">
            <h2 id="t-dec" className="mb-2 text-sm font-semibold">החלטות אחרונות</h2>
            <ol className="t-events">
              {(decisions.data?.decisions ?? []).slice(0, 12).map((d) => (
                <li key={d.bar_open_time}>
                  <span className="t-muted" dir="ltr">
                    {fmtUtc(d.bar_open_time).slice(5)}
                  </span>{" "}
                  {d.outcome === "ORDER" ? (
                    <span className="t-pos">כניסה {d.side}</span>
                  ) : d.outcome === "BLOCKED" ? (
                    <span className="t-warn">
                      {d.side} נחסם · {blockHe(d.block_reason)}
                    </span>
                  ) : d.outcome === "IN_POSITION" ? (
                    <span className="t-muted">בפוזיציה</span>
                  ) : (
                    <span className="t-muted">אין אות</span>
                  )}
                </li>
              ))}
              {decisions.data && decisions.data.decisions.length === 0 && <li className="t-muted">אין עדיין החלטות.</li>}
            </ol>
          </section>
        </aside>
      </div>

      <footer className="mt-6 text-center text-[11px] t-muted">
        כל המספרים מחושבים מיומן ה־PAPER (עמלת taker, החלקה לפי ספר הפקודות, funding). אינם תוצאות backtest ואינם ייעוץ
        השקעות.
      </footer>

      {dialog && CONFIRM[dialog] && (
        <div className="t-modal" role="dialog" aria-modal="true" aria-labelledby="t-modal-title">
          <div className="t-modal-box">
            <h3 id="t-modal-title" className="mb-2 text-base font-bold">
              {CONFIRM[dialog]!.title}
            </h3>
            <p className="mb-3 text-sm">{CONFIRM[dialog]!.body}</p>
            {dialog === "start" && (
              <label className="mb-3 flex items-start gap-2 text-sm">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-1" />
                <span>אני מבין שזה מסחר וירטואלי וניסיוני בלבד, ושאין הבטחה לרווח.</span>
              </label>
            )}
            <div className="flex gap-2">
              <button
                type="button"
                className={CONFIRM[dialog]!.danger ? "t-btn-kill flex-1" : "t-btn-start flex-1"}
                disabled={(dialog === "start" && (!ack || !startEnabled)) || !!busy}
                onClick={() => {
                  const a = dialog;
                  setDialog(null);
                  void run(a);
                }}
              >
                {CONFIRM[dialog]!.cta}
              </button>
              <button type="button" className="t-btn flex-1" onClick={() => setDialog(null)}>
                ביטול
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
