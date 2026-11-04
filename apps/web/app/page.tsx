import Link from "next/link";

import AutoRefresh from "./AutoRefresh";
import CandleChart from "./components/CandleChart";
import DataQuality from "./components/DataQuality";
import StatusPanel from "./components/StatusPanel";
import { explain } from "@/lib/i18n";
import { getDiscovery, getHealth, getMode, type DiscoveryState } from "@/lib/api";

// Always render from live backend state; never prerender static values.
export const dynamic = "force-dynamic";

function Card({ title, children }: { title: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="card">
      <h2 className="mb-3 text-base font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Row({ label, value, ltr }: { label: string; value: React.ReactNode; ltr?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1 text-sm">
      <dt style={{ color: "var(--muted)" }}>{label}</dt>
      <dd className="font-medium tabular-nums" dir={ltr ? "ltr" : undefined}>
        {value}
      </dd>
    </div>
  );
}

function Badge({ tone, children }: { tone: "ok" | "warn" | "bad"; children: React.ReactNode }) {
  return (
    <span className="rounded-full border px-2 py-0.5 text-xs font-semibold" style={{ color: `var(--${tone})`, borderColor: `var(--${tone})` }}>
      {children}
    </span>
  );
}

function ErrorText({ code }: { code: string }) {
  return (
    <p className="text-sm" style={{ color: "var(--bad)" }}>
      {explain(code)}
    </p>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} שניות`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} דקות`;
  return `${Math.round(seconds / 3600)} שעות`;
}

const STATE_BADGE: Record<DiscoveryState, { tone: "ok" | "warn" | "bad"; label: string }> = {
  CURRENT: { tone: "ok", label: "עדכני" },
  STALE: { tone: "warn", label: "נתונים ישנים" },
  FAILED: { tone: "bad", label: "האיתור האחרון נכשל" },
};

function MarketDetails({ data }: { data: import("@/lib/api").DiscoveryResponse }) {
  const d = data.discovery;
  const current = data.state === "CURRENT";
  const badge = STATE_BADGE[data.state];
  // Values that are not from a fresh, successful cycle are labelled "last known".
  const suffix = current ? "" : " (אחרון ידוע)";
  return (
    <>
      {!current && (
        <p
          role="alert"
          className="mb-3 rounded-lg border px-3 py-2 text-sm"
          style={{ color: `var(--${badge.tone})`, borderColor: `var(--${badge.tone})` }}
        >
          {data.state === "FAILED" && data.last_failure_after_discovery
            ? explain(data.last_failure_after_discovery.code)
            : "לא התקבל עדכון חדש מהשרת."}{" "}
          הערכים למטה הם הערכים האחרונים שנמדדו לפני {formatAge(data.age_seconds)}, ואינם מחירים
          עדכניים.
        </p>
      )}
      <dl style={current ? undefined : { opacity: 0.6 }}>
        <Row label="סטטוס" value={<Badge tone={badge.tone}>{badge.label}</Badge>} />
        <Row
          label={`מצב השוק${suffix}`}
          value={
            d.status === "ACTIVE" ? (
              <Badge tone="ok">פעיל</Badge>
            ) : (
              <Badge tone="warn">פגום חלקית</Badge>
            )
          }
        />
        <Row label={`מחיר Mark${suffix}`} value={d.mark_px} ltr />
        <Row label={`מחיר Oracle${suffix}`} value={d.oracle_px} ltr />
        <Row label="פער Mark/Oracle" value={`${d.oracle_mark_divergence_bps} bps`} ltr />
        <Row label="Funding (שעתי)" value={d.funding_rate} ltr />
        <Row label="מינוף מקסימלי בבורסה" value={`${d.max_leverage}x`} ltr />
        <Row label="דיוק כמות / מחיר" value={`${d.sz_decimals} / ${d.max_price_decimals}`} ltr />
        <Row
          label="זמן איתור (UTC)"
          value={new Date(d.discovered_at).toISOString().replace("T", " ").slice(0, 19)}
          ltr
        />
        <Row label="גיל הנתונים" value={formatAge(data.age_seconds)} />
      </dl>
      {d.warnings.length > 0 && (
        <ul className="mt-2 list-disc ps-5 text-xs" style={{ color: "var(--warn)" }} dir="ltr">
          {d.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-xs" style={{ color: "var(--muted)" }}>
        כניסות לעסקאות: חסומות. {explain(data.entries_blocked_reason)}
      </p>
    </>
  );
}

export default async function Home() {
  const [health, discovery, mode] = await Promise.all([getHealth(), getDiscovery(), getMode()]);
  const coin = discovery.ok
    ? discovery.data.discovery.coin
    : mode.ok
      ? mode.data.market.coin
      : null;
  const renderedAt = new Date().toISOString().replace("T", " ").slice(0, 19);

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
      <AutoRefresh seconds={60} />
      <header>
        <h1 className="text-2xl font-bold">AutoTrader AI</h1>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          מנוע נתוני שוק · נתונים ציבוריים מ־Hyperliquid Mainnet (קריאה בלבד). אין ביצוע עסקאות אמיתיות.
        </p>
        <nav className="mt-2 text-sm">
          <Link href="/backtest" className="underline">
            מעבדת Backtest (שלב 2) ←
          </Link>
        </nav>
      </header>

      <StatusPanel />
      <CandleChart />
      <DataQuality />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="מצב מערכת">
          {health.ok ? (
            <dl>
              <Row label="מצב מסחר" value={<Badge tone="ok">{health.data.trading_mode}</Badge>} />
              <Row label="מסחר אמיתי" value="לא קיים במערכת" />
              <Row
                label="מסד נתונים"
                value={
                  health.data.database === "ok" ? (
                    <Badge tone="ok">תקין</Badge>
                  ) : health.data.database === "not_configured" ? (
                    <Badge tone="warn">לא הוגדר</Badge>
                  ) : (
                    <Badge tone="bad">לא זמין</Badge>
                  )
                }
              />
              <Row label="גרסה" value={health.data.version} ltr />
            </dl>
          ) : (
            <ErrorText code={health.error} />
          )}
        </Card>

        <Card
          title={
            <>
              שוק <bdi dir="ltr">{coin ?? "—"}</bdi> · נפט <bdi dir="ltr">WTI</bdi> ·{" "}
              <bdi dir="ltr">HIP-3</bdi>
            </>
          }
        >
          {discovery.ok ? (
            <MarketDetails data={discovery.data} />
          ) : (
            <ErrorText code={discovery.error} />
          )}
        </Card>
      </div>

      <footer className="pb-6 text-center text-xs" style={{ color: "var(--muted)" }}>
        כל המחירים, העסקאות והרווח/הפסד במערכת זו הם סימולציה בלבד. אין חיבור לארנק ואין שליחת פקודות.
        <br />
        עודכן: <bdi dir="ltr">{renderedAt} UTC</bdi> · רענון אוטומטי כל 60 שניות
      </footer>
    </main>
  );
}
