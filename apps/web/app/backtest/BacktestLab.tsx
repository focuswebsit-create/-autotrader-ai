"use client";

import { Fragment, useEffect, useMemo, useState } from "react";

import { BLOCK_REASON, CRITERION, EXIT_REASON, SEGMENT, VERDICT, num, pct, reason, signed, tone } from "@/lib/bt";
import { getBackend } from "@/lib/client";
import { fmtUtc } from "@/lib/format";
import { explain } from "@/lib/i18n";
import type { BtTrade, Compact, Report, RunDetail, RunHeader, Verdict } from "@/lib/types";

import EquityChart from "./EquityChart";
import TradeChart from "./TradeChart";

function VerdictBadge({ v }: { v: Verdict }) {
  const t = VERDICT[v];
  return <span className={`badge badge-${t.tone === "ok" ? "ok" : t.tone === "warn" ? "warn" : "bad"}`}>{t.label}</span>;
}

function Card({ title, children, id }: { title: React.ReactNode; children: React.ReactNode; id?: string }) {
  return (
    <section className="card" aria-labelledby={id}>
      <h2 id={id} className="mb-3 text-base font-semibold">
        {title}
      </h2>
      {children}
    </section>
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

function pf(x: number | string | null): string {
  if (x == null) return "—";
  return typeof x === "number" ? x.toFixed(2) : x;
}

function ci(x: [number, number] | null): string {
  return x ? `[${x[0].toFixed(2)}, ${x[1].toFixed(2)}]` : "—";
}

function CompactRow({ name, c }: { name: React.ReactNode; c: Compact }) {
  return (
    <tr>
      <td>{name}</td>
      <td className="tabular-nums">{c.trades}</td>
      <td className="tabular-nums">{pct(c.win_rate)}</td>
      <td className="tabular-nums">{pf(c.profit_factor)}</td>
      <td className={`tabular-nums ${tone(c.net_pnl)}`} dir="ltr">
        {signed(c.net_pnl)}
      </td>
      <td className="tabular-nums" dir="ltr">
        {c.expectancy_r == null ? "—" : c.expectancy_r.toFixed(3)}
      </td>
      <td className="tabular-nums" dir="ltr">
        {ci(c.expectancy_r_ci95)}
      </td>
      <td className="tabular-nums" dir="ltr">
        {num(c.max_drawdown_pct)}%
      </td>
    </tr>
  );
}

function CompactHead({ first }: { first: string }) {
  return (
    <thead>
      <tr>
        <th>{first}</th>
        <th>עסקאות</th>
        <th>Win Rate</th>
        <th>PF</th>
        <th>נטו (USDC)</th>
        <th>תוחלת (R)</th>
        <th>רווח סמך 95%</th>
        <th>DD מקס׳</th>
      </tr>
    </thead>
  );
}

function Params({ p }: { p: Record<string, unknown> }) {
  return (
    <span dir="ltr" className="text-xs">
      {Object.entries(p)
        .filter(([, v]) => v != null)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(" · ")}
    </span>
  );
}

function TradeRow({ t, open, onToggle, onFocus }: { t: BtTrade; open: boolean; onToggle: () => void; onFocus: () => void }) {
  const flags = [
    t.ambiguous_bar && "נר דו־משמעי (סטופ קודם)",
    t.entry_offhours && "כניסה מחוץ לשעות CME",
    t.exit_offhours && "יציאה מחוץ לשעות CME",
    t.crossed_data_gap && "חצתה פער נתונים",
    t.funding_missing_hours > 0 && `${t.funding_missing_hours} שעות מימון חסרות (הונח מחיר שמרני)`,
  ].filter(Boolean) as string[];
  return (
    <Fragment>
      <tr>
        <td>
          <button type="button" className="underline" onClick={onFocus} aria-label={`הצג עסקה ${t.id} בגרף`}>
            #{t.id}
          </button>
        </td>
        <td className={t.side === "LONG" ? "ok" : "err"}>{t.side === "LONG" ? "לונג" : "שורט"}</td>
        <td dir="ltr">{fmtUtc(t.entry_time)}</td>
        <td className="tabular-nums" dir="ltr">
          {t.entry_price}
        </td>
        <td dir="ltr">{fmtUtc(t.exit_time)}</td>
        <td className="tabular-nums" dir="ltr">
          {t.exit_price}
        </td>
        <td>{EXIT_REASON[t.exit_reason] ?? t.exit_reason}</td>
        <td className="tabular-nums" dir="ltr">
          {t.size}
        </td>
        <td className={`tabular-nums ${tone(t.net_pnl)}`} dir="ltr">
          {signed(t.net_pnl, 3)}
        </td>
        <td className="tabular-nums" dir="ltr">
          {signed(t.r_multiple, 2)}R
        </td>
        <td>
          <button type="button" className="underline" aria-expanded={open} onClick={onToggle}>
            {open ? "הסתר" : "פרטים"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={11} className="detail">
            <div className="grid gap-3 py-2 sm:grid-cols-3">
              <dl className="text-xs">
                <div className="font-semibold">ביצוע (סימולציה)</div>
                <div>
                  אות בסגירת נר: <bdi dir="ltr">{fmtUtc(t.signal_time)} UTC</bdi>
                </div>
                <div>
                  כניסה בפתיחת הנר הבא: מחיר ייחוס <bdi dir="ltr">{t.entry_ref_price}</bdi> → מילוי{" "}
                  <bdi dir="ltr">{t.entry_price}</bdi>
                </div>
                <div>
                  סטופ <bdi dir="ltr">{t.stop}</bdi> · יעד <bdi dir="ltr">{t.target}</bdi> · ATR{" "}
                  <bdi dir="ltr">{t.atr}</bdi>
                </div>
                <div>
                  יציאה: ייחוס <bdi dir="ltr">{t.exit_ref_price}</bdi> → מילוי <bdi dir="ltr">{t.exit_price}</bdi> ·{" "}
                  {t.bars_held} נרות
                </div>
                <div>
                  שווי <bdi dir="ltr">{t.notional}</bdi> USDC · הון בכניסה <bdi dir="ltr">{num(t.equity_at_entry)}</bdi>
                </div>
              </dl>
              <dl className="text-xs">
                <div className="font-semibold">סיכון ועלויות (USDC)</div>
                <div>
                  תקציב סיכון <bdi dir="ltr">{num(t.risk_budget, 4)}</bdi> · הפסד מתוכנן בסטופ{" "}
                  <bdi dir="ltr">{num(t.planned_loss, 4)}</bdi>
                </div>
                <div>
                  ברוטו לפני עלויות <bdi dir="ltr">{signed(t.gross_pnl, 4)}</bdi>
                </div>
                <div>
                  עמלות <bdi dir="ltr">{num(t.fees, 4)}</bdi> · Spread <bdi dir="ltr">{num(t.spread_cost, 4)}</bdi> · Slippage{" "}
                  <bdi dir="ltr">{num(t.slippage_cost, 4)}</bdi>
                </div>
                <div>
                  Funding <bdi dir="ltr">{signed(t.funding, 4)}</bdi> ({t.funding_hours} שעות; חיובי = שולם)
                </div>
                <div className="font-semibold">
                  נטו <bdi dir="ltr">{signed(t.net_pnl, 4)}</bdi>
                </div>
                <div>
                  MAE <bdi dir="ltr">{t.mae}</bdi> · MFE <bdi dir="ltr">{t.mfe}</bdi>
                </div>
              </dl>
              <dl className="text-xs" dir="ltr" style={{ textAlign: "right" }}>
                <div className="font-semibold" dir="rtl">
                  נימוק האות (ערכי אינדיקטורים בנר הסגור)
                </div>
                {Object.entries(t.rationale).map(([k, v]) => (
                  <div key={k}>
                    {k}: {v}
                  </div>
                ))}
              </dl>
            </div>
            {flags.length > 0 && <p className="text-xs warn">{flags.join(" · ")}</p>}
          </td>
        </tr>
      )}
    </Fragment>
  );
}

/** Verdict reasons in Hebrew; the numeric detail of each criterion is in the table below. */
function heReasons(r: Report): string[] {
  const out: string[] = [];
  for (const x of r.verdict.reasons) {
    if (x.startsWith("no evidence of an edge")) out.push("אין עדות ליתרון: התוחלת המשוערת אחרי עלויות ≤ 0R.");
    else if (x.startsWith("after-cost expectancy is statistically negative"))
      out.push("התוחלת אחרי עלויות שלילית באופן מובהק (רווח סמך 95% כולו מתחת ל־0).");
    else if (x.startsWith("audit violations")) out.push("נמצאו הפרות בביקורת העצמאית — אין להשתמש בתוצאה.");
  }
  const failed = r.verdict.criteria.filter((c) => !c.passed).map((c) => CRITERION[c.criterion] ?? c.criterion);
  if (r.verdict.status !== "PASS" && failed.length) out.push(`קריטריונים שלא התקיימו: ${failed.join(" · ")}.`);
  return out;
}

function ReportView({ r, header }: { r: Report; header: RunHeader }) {
  const s = r.primary.summary;
  const v = VERDICT[r.verdict.status];
  const [focus, setFocus] = useState<number | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const trades = r.primary.trades;
  const visible = showAll ? trades : trades.slice(-25);
  const blocked = Object.entries(r.primary.decision_counts).filter(([k]) => k.startsWith("BLOCKED:"));
  const initial = Number((r.config.risk as Record<string, string>).initial_equity ?? 1000);

  return (
    <div className="flex flex-col gap-4">
      <section className={`card alert alert-${v.tone === "ok" ? "warn" : v.tone}`} style={{ color: "inherit" }} aria-live="polite">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-bold">תוצאה:</h2>
          <VerdictBadge v={r.verdict.status} />
          <span className="text-sm">{r.strategy.title_he}</span>
        </div>
        <p className="mt-2 text-sm">{v.explain}</p>
        {heReasons(r).length > 0 && (
          <ul className="mt-2 list-disc ps-5 text-sm">
            {heReasons(r).map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        )}
        <div className="table-wrap mt-3">
          <table className="tbl">
            <thead>
              <tr>
                <th>קריטריון להוכחת יתרון</th>
                <th>מצב</th>
                <th>פירוט</th>
              </tr>
            </thead>
            <tbody>
              {r.verdict.criteria.map((c) => (
                <tr key={c.criterion}>
                  <td>{CRITERION[c.criterion] ?? c.criterion}</td>
                  <td className={c.passed ? "ok" : "err"}>{c.passed ? "✓ עבר" : "✗ לא עבר"}</td>
                  <td dir="ltr" className="detail" style={{ textAlign: "right" }}>
                    {c.detail}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <Card title="מדדי ביצועים · פרמטרים קבועים מראש · כל התקופה · אחרי כל העלויות" id="kpi">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Kpi label="רווח/הפסד נטו (USDC)" value={signed(s.net_pnl)} cls={tone(s.net_pnl)} sub={`${signed(s.net_return_pct)}% מהון של ${num(initial, 0)}`} />
          <Kpi label="Win Rate" value={pct(s.win_rate)} sub={`${s.wins} רווח / ${s.losses} הפסד`} />
          <Kpi label="Profit Factor" value={pf(s.profit_factor)} sub="סכום רווחים / סכום הפסדים (נטו)" />
          <Kpi label="Maximum Drawdown" value={`${num(s.max_drawdown_pct)}%`} sub={`${num(s.max_drawdown)} USDC (סימון שמרני)`} cls="err" />
          <Kpi label="מספר עסקאות" value={s.trades} sub={`${s.longs} לונג · ${s.shorts} שורט · ${num(s.days, 0)} ימים`} />
          <Kpi label="תוחלת לעסקה" value={s.expectancy_r == null ? "—" : `${s.expectancy_r.toFixed(3)}R`} sub={`רווח סמך 95%: ${ci(s.expectancy_r_ci95)}`} cls={tone(s.expectancy_r)} />
          <Kpi label="ברוטו לפני עלויות" value={signed(s.gross_pnl_before_costs)} cls={tone(s.gross_pnl_before_costs)} sub={`עלויות כוללות ${num(s.total_costs)} USDC`} />
          <Kpi
            label="Sharpe / Sortino (יומי)"
            value={s.sharpe_daily_annualized == null ? "—" : `${s.sharpe_daily_annualized} / ${s.sortino_daily_annualized ?? "—"}`}
            sub={s.ratios_note ?? "מחושב על תשואות יומיות, שנתי (365)"}
          />
        </div>
        <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
          <dl className="sub">
            <div className="font-semibold">פירוק עלויות (USDC)</div>
            <div className="flex justify-between">
              <dt>עמלות</dt>
              <dd dir="ltr">{num(s.fees)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Spread</dt>
              <dd dir="ltr">{num(s.spread_cost)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Slippage</dt>
              <dd dir="ltr">{num(s.slippage_cost)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Funding (חיובי = שולם)</dt>
              <dd dir="ltr">{signed(s.funding_paid)}</dd>
            </div>
          </dl>
          <dl className="sub">
            <div className="font-semibold">פרטים נוספים</div>
            <div className="flex justify-between">
              <dt>רווח ממוצע / הפסד ממוצע</dt>
              <dd dir="ltr">
                {num(s.avg_win)} / {num(s.avg_loss)}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt>רצף הפסדים ארוך ביותר</dt>
              <dd>{s.longest_losing_streak}</dd>
            </div>
            <div className="flex justify-between">
              <dt>זמן בשוק</dt>
              <dd dir="ltr">{s.time_in_market_pct}%</dd>
            </div>
            <div className="flex justify-between">
              <dt>נרות דו־משמעיים (סטופ+יעד)</dt>
              <dd>{s.ambiguous_bars}</dd>
            </div>
            <div className="flex justify-between">
              <dt>סיבות יציאה</dt>
              <dd>
                {Object.entries(s.exit_reasons)
                  .map(([k, n]) => `${EXIT_REASON[k] ?? k}: ${n}`)
                  .join(" · ")}
              </dd>
            </div>
          </dl>
        </div>
      </Card>

      <Card title="גרף עם נקודות כניסה ויציאה · נרות 30 דקות סגורים" id="chart">
        <TradeChart trades={trades} start={r.trade_window.start} end={r.trade_window.end} focus={focus} />
      </Card>

      <Card title="Equity Curve · הון וירטואלי (USDC)" id="equity">
        <EquityChart points={r.primary.equity} initial={initial} />
        {r.primary.events.length > 0 && (
          <ul className="mt-2 list-disc ps-5 text-xs warn">
            {r.primary.events.map((e) => (
              <li key={`${e.time}-${e.event}`}>
                <bdi dir="ltr">{fmtUtc(e.time)}</bdi> · {BLOCK_REASON[e.event] ?? e.event}: <bdi dir="ltr">{e.detail}</bdi>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title={`היסטוריית עסקאות (${trades.length})`} id="trades">
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>#</th>
                <th>כיוון</th>
                <th>כניסה (UTC)</th>
                <th>מחיר כניסה</th>
                <th>יציאה (UTC)</th>
                <th>מחיר יציאה</th>
                <th>סיבה</th>
                <th>כמות</th>
                <th>נטו (USDC)</th>
                <th>R</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((t) => (
                <TradeRow
                  key={t.id}
                  t={t}
                  open={open === t.id}
                  onToggle={() => setOpen(open === t.id ? null : t.id)}
                  onFocus={() => {
                    setFocus(t.id);
                    document.getElementById("chart")?.scrollIntoView({ behavior: "smooth", block: "start" });
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
        {trades.length > 25 && (
          <button type="button" className="mt-2 text-sm underline" onClick={() => setShowAll(!showAll)}>
            {showAll ? "הצג רק 25 אחרונות" : `הצג את כל ${trades.length} העסקאות`}
          </button>
        )}
      </Card>

      <Card title="הפרדת אימון ובדיקה (כרונולוגית 70/15/15)" id="segments">
        <p className="mb-2 text-xs muted">אותם פרמטרים קבועים מראש בכל קטע. כל קטע מתחיל עם הון 1,000 ומשתמש בנתונים שלפניו רק לחימום אינדיקטורים.</p>
        <div className="table-wrap">
          <table className="tbl">
            <CompactHead first="קטע" />
            <tbody>
              {r.segments.map((x) => (
                <CompactRow
                  key={x.name}
                  c={x}
                  name={
                    <>
                      {SEGMENT[x.name] ?? x.name}
                      <div className="text-xs muted" dir="ltr">
                        {fmtUtc(x.start).slice(0, 10)} → {fmtUtc(x.end).slice(0, 10)}
                      </div>
                    </>
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
        <h3 className="mb-1 mt-4 text-sm font-semibold">אופטימיזציה על קטע הפיתוח בלבד ({r.optimization.grid_size} צירופים)</h3>
        <p className="text-xs muted">
          נבחר: <Params p={r.optimization.selected_params} />
        </p>
        <p className="mb-2 text-xs warn">תוצאות קטע הפיתוח של הפרמטרים שנבחרו הן בתוך המדגם ולכן אופטימיות. רק אימות ובדיקה סופית הן מחוץ למדגם.</p>
        <div className="table-wrap">
          <table className="tbl">
            <CompactHead first="קטע" />
            <tbody>
              {r.optimization.segments.map((x) => (
                <CompactRow key={x.name} c={x} name={SEGMENT[x.name] ?? x.name} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title={`Walk-Forward Testing · אימון ${r.walk_forward.train_days} ימים → בדיקה ${r.walk_forward.test_days} ימים`} id="wf">
        <p className="mb-2 text-sm">
          מצטבר מחוץ למדגם: {r.walk_forward.oos.trades} עסקאות · נטו{" "}
          <bdi dir="ltr" className={tone(r.walk_forward.oos.net_pnl)}>
            {signed(r.walk_forward.oos.net_pnl)}
          </bdi>{" "}
          USDC · Win Rate {pct(r.walk_forward.oos.win_rate)} · PF {pf(r.walk_forward.oos.profit_factor)} · תוחלת{" "}
          <bdi dir="ltr">{r.walk_forward.oos.expectancy_r?.toFixed(3) ?? "—"}R</bdi> · רווח סמך{" "}
          <bdi dir="ltr">{ci(r.walk_forward.oos.expectancy_r_ci95)}</bdi>
        </p>
        <div className="table-wrap">
          <table className="tbl">
            <CompactHead first="חלון בדיקה" />
            <tbody>
              {r.walk_forward.folds.map((f) => (
                <CompactRow
                  key={f.test_start}
                  c={f.test}
                  name={
                    <>
                      <span dir="ltr">
                        {fmtUtc(f.test_start).slice(0, 10)} → {fmtUtc(f.test_end).slice(0, 10)}
                      </span>
                      <div className="text-xs muted">
                        <Params p={f.params} />
                      </div>
                    </>
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="מבחני רגישות ולחץ" id="sens">
        <div className="table-wrap">
          <table className="tbl">
            <CompactHead first="תרחיש" />
            <tbody>
              {r.sensitivity.map((x) => (
                <CompactRow key={x.scenario} c={x} name={x.label_he} />
              ))}
            </tbody>
          </table>
        </div>
        <h3 className="mb-1 mt-4 text-sm font-semibold">רגישות לפרמטרים (±20%)</h3>
        <div className="table-wrap">
          <table className="tbl">
            <CompactHead first="פרמטר" />
            <tbody>
              {r.robustness.map((x) => (
                <CompactRow key={`${x.param}-${x.value}`} c={x} name={<bdi dir="ltr">{`${x.param}=${x.value}`}</bdi>} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="אותות שנחסמו ולמה" id="blocked">
        <p className="mb-2 text-xs muted">כל אות של האסטרטגיה נרשם. אות שלא עבר את בדיקות הסיכון, שעות המסחר או הביצוע — נחסם עם סיבה.</p>
        <ul className="grid gap-1 text-sm sm:grid-cols-2">
          <li>
            אותות: {r.primary.decision_counts.SIGNAL ?? 0} · בוצעו: {r.primary.decision_counts.FILLED ?? 0}
          </li>
          {blocked.map(([k, n]) => (
            <li key={k}>
              {reason(k)}: {n}
            </li>
          ))}
        </ul>
      </Card>

      <Card title="נתונים, הנחות ושחזור" id="data">
        <div className="grid gap-3 text-sm sm:grid-cols-2">
          <dl className="sub">
            <div className="font-semibold">נתונים (אמיתיים מ־Hyperliquid)</div>
            <div>
              30m: {r.data.m30.bars.toLocaleString("en-US")} נרות סגורים · <bdi dir="ltr">{fmtUtc(r.data.m30.first_open)} → {fmtUtc(r.data.m30.last_close)}</bdi> · חסרים: {r.data.m30.missing_bars}
            </div>
            <div>
              1h: {r.data.h1.bars.toLocaleString("en-US")} נרות · <bdi dir="ltr">{fmtUtc(r.data.h1.first_open)} → {fmtUtc(r.data.h1.last_close)}</bdi> · חסרים: {r.data.h1.missing_bars}
            </div>
            <div>
              Funding: {r.data.funding.records.toLocaleString("en-US")} רשומות שעתיות · {r.data.funding.provenance}
            </div>
            <div className="warn">{r.data.limitation}</div>
          </dl>
          <dl className="sub text-xs" dir="ltr" style={{ textAlign: "right" }}>
            <div className="font-semibold" dir="rtl">
              שחזור (Reproducibility)
            </div>
            <div>strategy: {r.strategy.id} v{r.strategy.version}</div>
            <div>params: {r.params_hash.slice(0, 16)}…</div>
            <div>data: {r.data.fingerprint_sha256.slice(0, 16)}…</div>
            <div>code: {r.code.commit.slice(0, 16)} · engine {r.code.engine_version}</div>
            <div>report: {r.report_sha256.slice(0, 16)}…</div>
            <div>
              run #{header.id} · {fmtUtc(header.created_at)} UTC · audit violations: {r.audit.violation_count}
            </div>
          </dl>
        </div>
        <h3 className="mb-1 mt-4 text-sm font-semibold">הנחות מול נתונים מאומתים</h3>
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>פריט</th>
                <th>ערך</th>
                <th>מקור</th>
                <th>הערה</th>
              </tr>
            </thead>
            <tbody>
              {r.assumptions.map((a) => (
                <tr key={a.item}>
                  <td dir="ltr">{a.item}</td>
                  <td dir="ltr">{a.value}</td>
                  <td>
                    <span className={`badge ${a.provenance === "VERIFIED" ? "badge-ok" : "badge-warn"}`}>
                      {a.provenance === "VERIFIED" ? "מאומת" : a.provenance === "ASSUMPTION" ? "הנחה" : "לא מאומת"}
                    </span>
                  </td>
                  <td className="detail" dir="ltr" style={{ textAlign: "right" }}>
                    {a.note}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs muted">
          פרמטרים: <Params p={r.params} />
        </p>
      </Card>
    </div>
  );
}

export default function BacktestLab() {
  const [runs, setRuns] = useState<RunHeader[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);

  useEffect(() => {
    void (async () => {
      const res = await getBackend<{ runs: RunHeader[] }>("backtest/runs", { limit: "100" });
      if (!res.ok) {
        setError(res.error);
        return;
      }
      setRuns(res.data.runs);
      if (res.data.runs.length) setSelected(res.data.runs[0].id);
    })();
  }, []);

  useEffect(() => {
    if (selected == null) return;
    let cancelled = false;
    void (async () => {
      const res = await getBackend<RunDetail>(`backtest/runs/${selected}`);
      if (cancelled) return;
      if (res.ok) {
        setDetail(res.data);
        setError(null);
      } else setError(res.error);
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // latest run per strategy, for the strategy picker
  const latest = useMemo(() => {
    const m = new Map<string, RunHeader>();
    for (const r of runs ?? []) if (!m.has(r.strategy_id)) m.set(r.strategy_id, r);
    return [...m.values()];
  }, [runs]);
  const loadingDetail = selected != null && detail?.header.id !== selected && !error;
  const current = runs?.find((r) => r.id === selected) ?? null;
  const history = runs?.filter((r) => r.strategy_id === current?.strategy_id) ?? [];

  return (
    <div className="flex flex-col gap-4">
      <section className="card">
        <h2 className="mb-2 text-base font-semibold">בחר אסטרטגיה</h2>
        {runs == null && !error && <p className="text-sm muted">טוען…</p>}
        {runs != null && runs.length === 0 && (
          <p className="text-sm warn">
            עדיין לא נשמרו הרצות. הרץ בשרת: <bdi dir="ltr">uv run autotrader backtest</bdi>
          </p>
        )}
        <div className="grid gap-2 sm:grid-cols-3" role="group" aria-label="אסטרטגיות">
          {latest.map((r) => (
            <button
              key={r.strategy_id}
              type="button"
              aria-pressed={current?.strategy_id === r.strategy_id}
              onClick={() => setSelected(r.id)}
              className="sub text-start"
              style={current?.strategy_id === r.strategy_id ? { borderColor: "var(--accent)", background: "var(--accent-bg)" } : undefined}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{r.title_he ?? r.strategy_id}</span>
                <VerdictBadge v={r.verdict} />
              </div>
              <div className="text-xs muted">
                {r.trades} עסקאות · נטו <bdi dir="ltr">{signed(r.net_pnl)}</bdi> USDC · PF {pf(r.profit_factor)}
              </div>
            </button>
          ))}
        </div>
        {history.length > 1 && (
          <label className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            הרצה:
            <select className="input" value={selected ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
              {history.map((h) => (
                <option key={h.id} value={h.id}>
                  #{h.id} · {fmtUtc(h.created_at)} UTC · {h.verdict}
                </option>
              ))}
            </select>
          </label>
        )}
        {error && <p className="mt-2 text-sm err">{explain(error)}</p>}
      </section>
      {loadingDetail && <p className="text-sm muted">טוען דוח…</p>}
      {detail && <ReportView key={detail.header.id} r={detail.report} header={detail.header} />}
    </div>
  );
}
