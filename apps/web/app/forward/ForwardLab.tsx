"use client";

import { Fragment, useEffect, useMemo, useState } from "react";

import { CATEGORY, CRITERION, EXIT_REASON, VERDICT, num, pct, reason, signed, tone } from "@/lib/bt";
import { getBackend } from "@/lib/client";
import { fmtUtc } from "@/lib/format";
import { explain } from "@/lib/i18n";
import type { ForwardDecision, ForwardReport, ForwardTestHeader, Verdict } from "@/lib/types";

import EquityChart from "../backtest/EquityChart";
import TradeChart from "../backtest/TradeChart";

const POLL_MS = 60_000;

const STATE: Record<string, { label: string; cls: string }> = {
  ACTIVE: { label: "פעיל", cls: "badge-ok" },
  STOPPED: { label: "נעצר", cls: "badge-warn" },
  INTEGRITY_HALT: { label: "נעצר: שינוי קוד", cls: "badge-bad" },
};

const FWD_CRITERION: Record<string, string> = {
  code_unchanged: "קוד האסטרטגיה לא השתנה מאז ההקפאה",
  live_replay_parity: "כל איתות שנרשם משתחזר בדיוק",
  zero_audit_violations: "אפס הפרות בביקורת",
  min_trades: "מספר עסקאות סגורות",
  min_days: "ימי מעקב",
  min_trade_days: "ימים שונים עם עסקה",
  expectancy_ci_above_zero: CRITERION.expectancy_ci_above_zero,
  survives_2x_costs: CRITERION.survives_2x_costs,
};

const METRIC: Record<string, string> = {
  expectancy_r: "תוחלת לעסקה (R)",
  win_rate: "Win Rate",
  profit_factor: "Profit Factor",
  trades_per_day: "עסקאות ליום",
  signals_per_day: "איתותים ליום",
  max_drawdown_pct: "Max Drawdown %",
};

const CONSISTENCY: Record<string, string> = {
  INSUFFICIENT_DATA: "אין מספיק עסקאות Forward להשוואה (נדרשות ≥10)",
  CONSISTENT: "תואם: תוחלת ה־Backtest בתוך רווח הסמך של ה־Forward",
  FORWARD_WORSE: "ה־Forward גרוע מה־Backtest באופן מובהק",
  FORWARD_BETTER: "ה־Forward טוב מה־Backtest באופן מובהק",
};

function fmtMetric(v: number | string | null | undefined, metric: string): string {
  if (v == null) return "—";
  if (metric === "win_rate" && typeof v === "number") return pct(v);
  return typeof v === "number" ? String(Number(v.toFixed(4))) : String(v);
}

function Progress({ label, value, target }: { label: string; value: number; target: number }) {
  const ratio = Math.min(1, target > 0 ? value / target : 0);
  return (
    <div>
      <div className="flex justify-between text-xs">
        <span>{label}</span>
        <span dir="ltr">
          {Number.isInteger(value) ? value : value.toFixed(1)} / {target}
        </span>
      </div>
      <div className="mt-1 h-2 w-full overflow-hidden rounded-full" style={{ background: "var(--grid)" }} role="progressbar" aria-valuenow={Math.round(ratio * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
        <div className="h-2" style={{ width: `${ratio * 100}%`, background: ratio >= 1 ? "var(--ok)" : "var(--accent)" }} />
      </div>
    </div>
  );
}

function Kpi({ label, value, sub, cls }: { label: string; value: React.ReactNode; sub?: React.ReactNode; cls?: string }) {
  return (
    <div className="sub">
      <div className="text-xs muted">{label}</div>
      <div className={`text-lg font-bold tabular-nums ${cls ?? ""}`} dir="ltr" style={{ textAlign: "right" }}>
        {value}
      </div>
      {sub != null && <div className="text-xs muted">{sub}</div>}
    </div>
  );
}

function SignalRow({ d, outcome, open, onToggle }: { d: ForwardDecision; outcome?: { status: string; reason: string; category: string; trade_id: number | null }; open: boolean; onToggle: () => void }) {
  return (
    <Fragment>
      <tr>
        <td dir="ltr">{fmtUtc(d.bar_close_time)}</td>
        <td className={d.side === "LONG" ? "ok" : "err"}>{d.side === "LONG" ? "לונג" : "שורט"}</td>
        <td>
          {outcome ? (
            outcome.status === "FILLED" ? (
              <span className="ok">בוצע · עסקה #{outcome.trade_id}</span>
            ) : outcome.status === "PENDING" ? (
              <span className="warn">ממתין לנר הבא</span>
            ) : (
              <span>
                נחסם: {reason(outcome.reason)}
                <span className="muted"> ({CATEGORY[outcome.category] ?? outcome.category})</span>
              </span>
            )
          ) : (
            <span className="muted">לא נבדק — פוזיציה פתוחה</span>
          )}
        </td>
        <td className="tabular-nums" dir="ltr">
          {d.market.bar.close}
        </td>
        <td dir="ltr">{fmtUtc(d.evaluated_at, true)}</td>
        <td>{d.late ? <span className="warn">השלמה</span> : "בזמן"}</td>
        <td>
          <button type="button" className="underline" aria-expanded={open} onClick={onToggle}>
            {open ? "הסתר" : "פרטים"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7} className="detail">
            <div className="grid gap-3 py-2 text-xs sm:grid-cols-2">
              <dl dir="ltr" style={{ textAlign: "right" }}>
                <div className="font-semibold" dir="rtl">
                  נתוני שוק בזמן האיתות
                </div>
                <div>
                  O {d.market.bar.open} · H {d.market.bar.high} · L {d.market.bar.low} · C {d.market.bar.close}
                </div>
                <div>
                  Vol {d.market.bar.volume} · trades {d.market.bar.trades ?? "—"}
                </div>
                <div>funding (last) {d.market.funding_rate_last ?? "—"}</div>
                <div>
                  mark {d.market.mark_px ?? "—"} · oracle {d.market.oracle_px ?? "—"} · div {d.market.oracle_mark_divergence_bps ?? "—"} bps
                </div>
                <div>
                  version {d.strategy_version} · params {d.params_hash.slice(0, 12)}… · input {d.input_digest.slice(0, 12)}…
                </div>
              </dl>
              <dl dir="ltr" style={{ textAlign: "right" }}>
                <div className="font-semibold" dir="rtl">
                  נימוק ההחלטה
                </div>
                {Object.entries(d.rationale).map(([k, v]) => (
                  <div key={k}>
                    {k}: {v}
                  </div>
                ))}
              </dl>
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function Report({ r }: { r: ForwardReport }) {
  const v = VERDICT[r.verdict.status as Verdict];
  const s = r.summary;
  const th = r.test.thresholds;
  const [openSig, setOpenSig] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const outcomes = useMemo(() => new Map(r.engine_signals.map((e) => [e.time, e])), [r.engine_signals]);
  const initial = Number(r.test.risk.initial_equity ?? 1000);
  const sigs = [...r.signals].reverse();

  return (
    <div className="flex flex-col gap-4">
      <section className={`card alert alert-${v.tone === "ok" ? "warn" : v.tone}`} style={{ color: "inherit" }}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-bold">תוצאת Forward:</h2>
          <span className={`badge badge-${v.tone === "ok" ? "ok" : v.tone === "warn" ? "warn" : "bad"}`}>{v.label}</span>
          <span className={`badge ${STATE[r.test.state]?.cls ?? ""}`}>{STATE[r.test.state]?.label ?? r.test.state}</span>
          <span className="text-sm">{r.test.strategy.title_he}</span>
        </div>
        <p className="mt-2 text-sm">
          {r.verdict.status === "INCONCLUSIVE"
            ? "עדיין אין מספיק תצפיות בלתי תלויות על נתונים חדשים כדי לקבוע אם לאסטרטגיה יש יתרון. אין להסיק רווחיות."
            : v.explain}
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <Progress label="עסקאות סגורות" value={r.progress.closed_trades} target={th.min_trades} />
          <Progress label="ימי מעקב" value={r.progress.observed_days} target={th.min_days} />
          <Progress label="ימים שונים עם עסקה" value={r.progress.trade_days} target={th.min_trade_days} />
        </div>
        <div className="table-wrap mt-3">
          <table className="tbl">
            <tbody>
              {r.verdict.criteria.map((c) => (
                <tr key={c.criterion}>
                  <td>{FWD_CRITERION[c.criterion] ?? c.criterion}</td>
                  <td className={c.passed ? "ok" : "err"}>{c.passed ? "✓" : "✗"}</td>
                  <td dir="ltr" className="detail" style={{ textAlign: "right" }}>
                    {c.detail}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {r.progress.data_stale && (
        <p role="alert" className="alert alert-bad">
          נתוני השוק לא מתעדכנים (הנר הסגור האחרון: <bdi dir="ltr">{fmtUtc(r.progress.last_closed_bar)} UTC</bdi>). שירות
          האיסוף אינו פעיל, ולכן לא נרשמים איתותים חדשים.
        </p>
      )}

      <section className="card">
        <h2 className="mb-3 text-base font-semibold">רווח והפסד וירטואליים (מאז תחילת הבדיקה, אחרי כל העלויות)</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Kpi label="רווח/הפסד נטו (USDC)" value={signed(s?.net_pnl ?? 0)} cls={tone(s?.net_pnl)} sub={`עסקאות סגורות · הון ${num(initial, 0)}`} />
          <Kpi label="Win Rate" value={pct(s?.win_rate)} sub={`${s?.wins ?? 0} רווח / ${s?.losses ?? 0} הפסד`} />
          <Kpi label="Profit Factor" value={s?.profit_factor == null ? "—" : String(s.profit_factor)} />
          <Kpi label="Maximum Drawdown" value={`${num(s?.max_drawdown_pct ?? 0)}%`} cls="err" sub={`${num(s?.max_drawdown ?? 0)} USDC`} />
          <Kpi label="עסקאות" value={r.progress.closed_trades} sub={`${r.progress.signals} איתותים · ${r.progress.decisions} נרות נבדקו`} />
          <Kpi label="תוחלת (R)" value={r.stats.expectancy_r == null ? "—" : r.stats.expectancy_r.toFixed(3)} sub={r.stats.expectancy_r_ci95 ? `CI ${r.stats.expectancy_r_ci95.map((x) => x.toFixed(2)).join(" … ")}` : "אין מספיק עסקאות"} />
          <Kpi label="נטו בעלויות כפולות" value={signed(r.stress_2x_costs_net ?? 0)} cls={tone(r.stress_2x_costs_net)} />
          <Kpi
            label="פוזיציה פתוחה"
            value={r.open_position ? `${r.open_position.side === "LONG" ? "לונג" : "שורט"} ${r.open_position.size}` : r.pending_signal ? "ממתין לכניסה" : "אין"}
            sub={r.open_position ? `לא ממומש ${signed(r.open_position.unrealized_pnl)} · סטופ ${r.open_position.stop} · יעד ${r.open_position.target}` : undefined}
          />
        </div>
        {s && (
          <p className="mt-2 text-xs muted">
            עלויות: עמלות {num(s.fees)} · Spread {num(s.spread_cost)} · Slippage {num(s.slippage_cost)} · Funding {signed(s.funding_paid)} USDC
          </p>
        )}
      </section>

      <section className="card" id="fwd-chart">
        <h2 className="mb-3 text-base font-semibold">גרף · כניסות ויציאות בתקופת הבדיקה</h2>
        <TradeChart trades={r.trades} start={r.test.starts_at} end={r.progress.now} focus={focus} />
      </section>

      {r.equity.length > 1 && (
        <section className="card">
          <h2 className="mb-3 text-base font-semibold">Equity Curve וירטואלית</h2>
          <EquityChart points={r.equity} initial={initial} />
        </section>
      )}

      <section className="card">
        <h2 className="mb-3 text-base font-semibold">השוואה: Backtest מול Forward</h2>
        {r.comparison.available ? (
          <>
            <p className="mb-2 text-sm">
              {CONSISTENCY[r.comparison.expectancy_consistency ?? "INSUFFICIENT_DATA"]}
            </p>
            <div className="table-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>מדד</th>
                    <th>Backtest (הרצה #{r.comparison.reference_run_id})</th>
                    <th>Forward</th>
                  </tr>
                </thead>
                <tbody>
                  {r.comparison.metrics?.map((m) => (
                    <tr key={m.metric}>
                      <td>{METRIC[m.metric] ?? m.metric}</td>
                      <td dir="ltr">{fmtMetric(m.backtest, m.metric)}</td>
                      <td dir="ltr">{fmtMetric(m.forward, m.metric)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="text-sm muted">לא נמצאה הרצת Backtest עם אותם פרמטרים בזמן ההקפאה.</p>
        )}
        <p className="mt-2 text-xs">
          התאמת שחזור (Parity): {r.parity.checked} איתותים נבדקו מחדש בקוד ה־Backtest ·{" "}
          <span className={r.parity.mismatches ? "err" : "ok"}>{r.parity.mismatches} אי־התאמות</span>
        </p>
        <p className="mt-1 text-xs muted">{r.comparison.note}</p>
      </section>

      <section className="card">
        <h2 className="mb-3 text-base font-semibold">היסטוריית איתותים ({r.signals.length})</h2>
        {sigs.length === 0 ? (
          <p className="text-sm muted">עדיין לא נוצרו איתותים מאז תחילת הבדיקה.</p>
        ) : (
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>נר נסגר (UTC)</th>
                  <th>כיוון</th>
                  <th>תוצאה</th>
                  <th>סגירה</th>
                  <th>נרשם ב־</th>
                  <th>תזמון</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {sigs.map((d) => (
                  <SignalRow
                    key={d.bar_open_time}
                    d={d}
                    outcome={outcomes.get(d.bar_close_time)}
                    open={openSig === d.bar_open_time}
                    onToggle={() => setOpenSig(openSig === d.bar_open_time ? null : d.bar_open_time)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {r.trades.length > 0 && (
        <section className="card">
          <h2 className="mb-3 text-base font-semibold">עסקאות וירטואליות ({r.trades.length})</h2>
          <div className="table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>#</th>
                  <th>כיוון</th>
                  <th>כניסה</th>
                  <th>יציאה</th>
                  <th>סיבה</th>
                  <th>נטו</th>
                  <th>R</th>
                </tr>
              </thead>
              <tbody>
                {[...r.trades].reverse().map((t) => (
                  <tr key={t.id}>
                    <td>
                      <button type="button" className="underline" onClick={() => { setFocus(t.id); document.getElementById("fwd-chart")?.scrollIntoView({ behavior: "smooth" }); }}>
                        #{t.id}
                      </button>
                    </td>
                    <td className={t.side === "LONG" ? "ok" : "err"}>{t.side === "LONG" ? "לונג" : "שורט"}</td>
                    <td dir="ltr">
                      {fmtUtc(t.entry_time)} @ {t.entry_price}
                    </td>
                    <td dir="ltr">
                      {fmtUtc(t.exit_time)} @ {t.exit_price}
                    </td>
                    <td>{EXIT_REASON[t.exit_reason] ?? t.exit_reason}</td>
                    <td className={tone(t.net_pnl)} dir="ltr">
                      {signed(t.net_pnl, 3)}
                    </td>
                    <td dir="ltr">{signed(t.r_multiple, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="card text-xs">
        <h2 className="mb-2 text-base font-semibold">הקפאה ושלמות הבדיקה</h2>
        <dl className="grid gap-1 sm:grid-cols-2" dir="ltr" style={{ textAlign: "right" }}>
          <div>strategy {r.test.strategy.id} v{r.test.strategy.version}</div>
          <div>params {r.test.params_hash.slice(0, 16)}…</div>
          <div>
            code {r.test.code_hash.slice(0, 16)}… {r.test.code_hash_matches ? "✓ unchanged" : "✗ CHANGED"}
          </div>
          <div>config {r.test.config_sha256.slice(0, 16)}…</div>
          <div>frozen {fmtUtc(r.test.created_at)} · starts {fmtUtc(r.test.starts_at)} UTC</div>
          <div>commit {r.test.code_version.slice(0, 12)}</div>
        </dl>
        <p className="mt-2 muted">
          ההגדרות, האיתותים והאירועים נשמרים במסד הנתונים כרשומות שלא ניתן לעדכן או למחוק. התוצאות מחושבות מחדש בכל טעינה
          מתוך האיתותים שנרשמו בזמן אמת, ולכן אין אפשרות לשנות אותן בדיעבד. שינוי בקוד האסטרטגיה עוצר את הבדיקה.
        </p>
        <p className="mt-2 muted" dir="ltr" style={{ textAlign: "right" }}>
          {Object.entries(r.test.params)
            .map(([k, val]) => `${k}=${String(val)}`)
            .join(" · ")}
        </p>
        {r.events.length > 0 && (
          <ul className="mt-2 list-disc ps-5">
            {r.events.map((e, i) => (
              <li key={i}>
                <bdi dir="ltr">{fmtUtc(e.time)}</bdi> · {e.event} <bdi dir="ltr">{JSON.stringify(e.detail)}</bdi>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export default function ForwardLab() {
  const [tests, setTests] = useState<ForwardTestHeader[] | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [report, setReport] = useState<ForwardReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      const res = await getBackend<{ tests: ForwardTestHeader[] }>("forward/tests");
      if (!res.ok) return setError(res.error);
      setTests(res.data.tests);
      const first = res.data.tests.find((t) => t.state === "ACTIVE") ?? res.data.tests[0];
      if (first) setSelected(first.id);
    })();
  }, []);

  useEffect(() => {
    if (selected == null) return;
    let cancelled = false;
    const load = async () => {
      const res = await getBackend<ForwardReport>(`forward/tests/${selected}`);
      if (cancelled) return;
      if (res.ok) {
        setReport(res.data);
        setError(null);
      } else setError(res.error);
    };
    const first = window.setTimeout(() => void load(), 0);
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [selected]);

  const loading = selected != null && report?.test.id !== selected && !error;

  return (
    <div className="flex flex-col gap-4">
      <section className="card">
        <h2 className="mb-2 text-base font-semibold">בדיקות Forward</h2>
        {tests == null && !error && <p className="text-sm muted">טוען…</p>}
        {tests != null && tests.length === 0 && (
          <p className="text-sm warn">
            עדיין לא הוקפאה בדיקה. בשרת: <bdi dir="ltr">uv run autotrader forward freeze</bdi> ואז{" "}
            <bdi dir="ltr">uv run autotrader forward run</bdi>
          </p>
        )}
        <div className="grid gap-2 sm:grid-cols-3" role="group" aria-label="בדיקות">
          {tests?.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-pressed={selected === t.id}
              onClick={() => setSelected(t.id)}
              className="sub text-start"
              style={selected === t.id ? { borderColor: "var(--accent)", background: "var(--accent-bg)" } : undefined}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{t.title_he ?? t.strategy_id}</span>
                <span className={`badge ${STATE[t.state]?.cls ?? ""}`}>{STATE[t.state]?.label ?? t.state}</span>
              </div>
              <div className="text-xs muted">
                #{t.id} · מאז <bdi dir="ltr">{fmtUtc(t.starts_at)} UTC</bdi>
              </div>
            </button>
          ))}
        </div>
        {error && <p className="mt-2 text-sm err">{explain(error)}</p>}
      </section>
      {loading && <p className="text-sm muted">מחשב תוצאות…</p>}
      {report && report.test.id === selected && <Report r={report} />}
    </div>
  );
}
