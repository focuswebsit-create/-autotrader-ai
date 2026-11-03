"use client";

import { ageFrom, fmtAge, fmtInt, fmtNum, fmtUtc } from "@/lib/format";
import { explain, explainAlert, SERVICE_STATES, WS_STATES } from "@/lib/i18n";
import type { CollectorStatus } from "@/lib/types";

import { useNow, usePoll } from "./usePoll";

const TONE: Record<string, "ok" | "warn" | "bad"> = {
  RUNNING: "ok",
  CONNECTED: "ok",
  DEGRADED: "warn",
  STARTING: "warn",
  CONNECTING: "warn",
  STALE: "warn",
  DOWN: "bad",
  STOPPED: "bad",
  NOT_STARTED: "bad",
  DISCONNECTED: "bad",
};

function Pill({ tone, children }: { tone: "ok" | "warn" | "bad"; children: React.ReactNode }) {
  return (
    <span className={`pill pill-${tone}`}>
      <span className="dot" aria-hidden />
      {children}
    </span>
  );
}

export default function StatusPanel() {
  const { data, error, at } = usePoll<CollectorStatus>("collector/status", 10_000);
  const now = useNow();
  const hb = data?.heartbeat ?? null;
  // If the backend itself stops answering, older values must not look live.
  const pollAge = at ? (now - at) / 1000 : null;
  const frozen = error != null || (pollAge != null && pollAge > 30);

  const service = data?.service_state ?? null;
  const ws = service === "RUNNING" || service === "DEGRADED" ? hb?.ws_state ?? "DISCONNECTED" : "DISCONNECTED";
  const ctx = hb?.market_ctx ?? {};
  const ctxAge = ageFrom(ctx.received_at, now);

  return (
    <section className="card" aria-labelledby="status-title">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id="status-title" className="text-base font-semibold">מצב איסוף נתונים</h2>
        {data && !frozen ? (
          <div className="flex flex-wrap gap-2">
            <Pill tone={TONE[service ?? "DOWN"] ?? "bad"}>שירות: {SERVICE_STATES[service ?? "DOWN"]}</Pill>
            <Pill tone={TONE[ws] ?? "bad"}>WebSocket: {WS_STATES[ws]}</Pill>
          </div>
        ) : (
          <Pill tone="bad">{error ? explain(error) : "טוען…"}</Pill>
        )}
      </div>

      {data && (
        <div style={frozen ? { opacity: 0.5 } : undefined}>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-3">
            <Stat label="מחיר Mark (חי)" value={ctx.markPx ? fmtNum(ctx.markPx) : "—"} ltr />
            <Stat label="מחיר Oracle (חי)" value={ctx.oraclePx ? fmtNum(ctx.oraclePx) : "—"} ltr />
            <Stat label="עדכון מחיר אחרון" value={ctxAge != null ? `לפני ${fmtAge(ctxAge)}` : "—"} />
            <Stat label="הודעה חיה אחרונה" value={hb?.last_ws_message_at ? `לפני ${fmtAge(ageFrom(hb.last_ws_message_at, now))}` : "—"} />
            <Stat label="דיווח שירות אחרון" value={hb?.last_heartbeat_at ? `לפני ${fmtAge(ageFrom(hb.last_heartbeat_at, now))}` : "—"} />
            <Stat label="התחברויות מחדש" value={fmtInt(hb?.reconnects)} ltr />
            <Stat label="הודעות שהתקבלו" value={fmtInt(hb?.messages)} ltr />
            <Stat label="הודעות ישנות שנדחו" value={fmtInt(hb?.ignored_out_of_order)} ltr />
            <Stat label="סנכרון REST אחרון" value={hb?.last_sync_at ? `${fmtUtc(hb.last_sync_at, true)}${hb.last_sync_ok === false ? " · נכשל" : ""}` : "—"} ltr />
          </dl>

          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {data.intervals.map((iv) => (
              <div key={iv.interval} className="sub">
                <div className="mb-1 flex items-center justify-between">
                  <bdi dir="ltr" className="font-semibold">{iv.interval}</bdi>
                  {iv.fresh ? <span className="badge badge-ok">מעודכן</span> : <span className="badge badge-warn">לא מעודכן</span>}
                </div>
                <dl className="text-xs">
                  <Stat label="נרות סגורים שמורים" value={fmtInt(iv.closed_candles)} ltr />
                  <Stat label="נר סגור אחרון" value={fmtUtc(iv.last_closed_open_time)} ltr />
                  <Stat label="פערים פתוחים" value={fmtInt(iv.open_gaps)} ltr />
                </dl>
              </div>
            ))}
          </div>

          <div className="mt-3" aria-live="polite">
            {data.alerts.length === 0 ? (
              <p className="text-sm ok">אין התראות פעילות.</p>
            ) : (
              <ul className="space-y-1">
                {data.alerts.map((a, i) => (
                  <li key={`${a.code}-${a.interval ?? ""}-${i}`} className={`alert alert-${a.severity === "ERROR" ? "bad" : "warn"}`}>
                    {explainAlert(a)}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {frozen && <p className="mt-2 text-xs err">הנתונים בכרטיס זה אינם מתעדכנים כרגע.</p>}
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, ltr }: { label: string; value: React.ReactNode; ltr?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <dt className="muted">{label}</dt>
      <dd className="font-medium tabular-nums" dir={ltr ? "ltr" : undefined}>{value}</dd>
    </div>
  );
}
