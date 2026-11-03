"use client";

import { useState } from "react";

import { fmtDate, fmtInt, fmtUtc } from "@/lib/format";
import { explain, ISSUE_TYPES } from "@/lib/i18n";
import type { DataReport, Issue } from "@/lib/types";

import { usePoll } from "./usePoll";

export default function DataQuality() {
  const [showResolved, setShowResolved] = useState(false);
  const report = usePoll<DataReport>("data/report", 60_000);
  const issues = usePoll<{ issues: Issue[] }>("data/issues", 60_000, {
    include_resolved: String(showResolved),
    limit: "200",
  });
  const r = report.data;

  return (
    <section className="card" aria-labelledby="dq-title">
      <h2 id="dq-title" className="mb-1 text-base font-semibold">איכות נתונים והיסטוריה</h2>
      <p className="mb-3 text-xs muted">
        מקור: Hyperliquid Mainnet (API ציבורי, קריאה בלבד). ה־API מספק רק כ־5,000 נרות אחרונים לכל טווח זמן. היסטוריה
        ישנה יותר אינה זמינה ממקור זה ואינה מושלמת באופן מלאכותי.
      </p>
      {report.error && !r && <p className="text-sm err">{explain(report.error)}</p>}

      {r && (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>טווח</th>
                <th>נרות סגורים</th>
                <th>מתאריך</th>
                <th>עד תאריך</th>
                <th>ימים</th>
                <th>נרות חסרים</th>
                <th>פערים</th>
              </tr>
            </thead>
            <tbody>
              {r.intervals.map((iv) => (
                <tr key={iv.interval}>
                  <td dir="ltr">{iv.interval}</td>
                  <td dir="ltr">{fmtInt(iv.closed_candles)}</td>
                  <td dir="ltr">{fmtDate(iv.first_open_time)}</td>
                  <td dir="ltr">{fmtUtc(iv.last_open_time)}</td>
                  <td dir="ltr">{iv.span_days}</td>
                  <td dir="ltr" className={iv.missing_bars ? "err" : "ok"}>{fmtInt(iv.missing_bars)}</td>
                  <td dir="ltr" className={iv.open_gaps ? "err" : "ok"}>{fmtInt(iv.open_gaps)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-4 mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">בעיות איכות שתועדו</h3>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          הצג גם בעיות שנפתרו
        </label>
      </div>
      {issues.data && issues.data.issues.length === 0 && (
        <p className="text-sm ok">לא תועדו בעיות {showResolved ? "" : "פתוחות "}בנתונים.</p>
      )}
      {issues.data && issues.data.issues.length > 0 && (
        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>סוג</th>
                <th>טווח</th>
                <th>נר</th>
                <th>פירוט</th>
                <th>זוהה</th>
                <th>סטטוס</th>
              </tr>
            </thead>
            <tbody>
              {issues.data.issues.map((i) => (
                <tr key={i.id}>
                  <td className={i.severity === "ERROR" ? "err" : i.severity === "WARNING" ? "warn" : ""}>{ISSUE_TYPES[i.type] ?? i.type}</td>
                  <td dir="ltr">{i.interval}</td>
                  <td dir="ltr">{fmtUtc(i.open_time)}{i.missing_bars ? ` (+${i.missing_bars})` : ""}</td>
                  <td dir="ltr" className="detail">{i.detail}</td>
                  <td dir="ltr">{fmtUtc(i.last_detected_at)}{i.occurrences > 1 ? ` ×${i.occurrences}` : ""}</td>
                  <td>{i.resolved_at ? "נפתר" : "פתוח"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {issues.error && !issues.data && <p className="text-sm err">{explain(issues.error)}</p>}

      {r && r.recent_ingestion_runs.length > 0 && (
        <details className="mt-4">
          <summary className="cursor-pointer text-sm font-semibold">היסטוריית איסוף (REST)</summary>
          <div className="table-wrap mt-2">
            <table className="tbl">
              <thead>
                <tr>
                  <th>#</th>
                  <th>טווח</th>
                  <th>סוג</th>
                  <th>סטטוס</th>
                  <th>התקבלו</th>
                  <th>נוספו</th>
                  <th>עודכנו</th>
                  <th>נדחו</th>
                  <th>זמן</th>
                </tr>
              </thead>
              <tbody>
                {r.recent_ingestion_runs.map((run) => (
                  <tr key={run.id}>
                    <td dir="ltr">{run.id}</td>
                    <td dir="ltr">{run.interval}</td>
                    <td dir="ltr">{run.kind}</td>
                    <td className={run.status === "FAILED" ? "err" : "ok"} title={run.error ?? undefined}>{run.status === "OK" ? "הצליח" : run.status === "FAILED" ? "נכשל" : "רץ"}</td>
                    <td dir="ltr">{fmtInt(run.received)}</td>
                    <td dir="ltr">{fmtInt(run.inserted)}</td>
                    <td dir="ltr">{fmtInt(run.updated)}</td>
                    <td dir="ltr">{fmtInt(run.rejected)}</td>
                    <td dir="ltr">{fmtUtc(run.started_at, true)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  );
}
