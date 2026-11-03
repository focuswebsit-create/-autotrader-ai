import { explain } from "@/lib/i18n";
import { getDiscovery, getHealth } from "@/lib/api";

// Always render from live backend state; never prerender static values.
export const dynamic = "force-dynamic";

function Card({ title, children }: { title: React.ReactNode; children: React.ReactNode }) {
  return (
    <section
      className="rounded-2xl border p-4 shadow-sm"
      style={{ background: "var(--card)", borderColor: "var(--border)" }}
    >
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

export default async function Home() {
  const [health, discovery] = await Promise.all([getHealth(), getDiscovery()]);

  return (
    <main className="mx-auto flex max-w-xl flex-col gap-4 px-4 py-6">
      <header>
        <h1 className="text-2xl font-bold">AutoTrader AI</h1>
        <p className="text-sm" style={{ color: "var(--muted)" }}>
          שלב 0 · איתור שוק ובדיקות בטיחות · נתונים ציבוריים מ־Hyperliquid (קריאה בלבד)
        </p>
      </header>

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
            שוק <bdi dir="ltr">xyz:CL</bdi> · נפט <bdi dir="ltr">WTI</bdi> · <bdi dir="ltr">HIP-3</bdi>
          </>
        }
      >
        {discovery.ok ? (
          <>
            <dl>
              <Row
                label="סטטוס"
                value={
                  discovery.data.stale ? (
                    <Badge tone="warn">נתונים ישנים</Badge>
                  ) : discovery.data.discovery.status === "ACTIVE" ? (
                    <Badge tone="ok">פעיל</Badge>
                  ) : (
                    <Badge tone="warn">פגום חלקית</Badge>
                  )
                }
              />
              <Row label="מחיר Mark" value={discovery.data.discovery.mark_px} ltr />
              <Row label="מחיר Oracle" value={discovery.data.discovery.oracle_px} ltr />
              <Row label="פער Mark/Oracle" value={`${discovery.data.discovery.oracle_mark_divergence_bps} bps`} ltr />
              <Row label="Funding (שעתי)" value={discovery.data.discovery.funding_rate} ltr />
              <Row label="מינוף מקסימלי בבורסה" value={`${discovery.data.discovery.max_leverage}x`} ltr />
              <Row label="דיוק כמות / מחיר" value={`${discovery.data.discovery.sz_decimals} / ${discovery.data.discovery.max_price_decimals}`} ltr />
              <Row
                label="זמן איתור (UTC)"
                value={new Date(discovery.data.discovery.discovered_at).toISOString().replace("T", " ").slice(0, 19)}
                ltr
              />
            </dl>
            {discovery.data.discovery.warnings.length > 0 && (
              <ul className="mt-2 list-disc ps-5 text-xs" style={{ color: "var(--warn)" }} dir="ltr">
                {discovery.data.discovery.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            {discovery.data.last_failure_after_discovery && (
              <ErrorText code={discovery.data.last_failure_after_discovery.code} />
            )}
            <p className="mt-3 text-xs" style={{ color: "var(--muted)" }}>
              כניסות לעסקאות: חסומות. {explain(discovery.data.entries_blocked_reason)}
            </p>
          </>
        ) : (
          <ErrorText code={discovery.error} />
        )}
      </Card>

      <footer className="pb-6 text-center text-xs" style={{ color: "var(--muted)" }}>
        כל המחירים, העסקאות והרווח/הפסד במערכת זו הם סימולציה בלבד. אין חיבור לארנק ואין שליחת פקודות.
      </footer>
    </main>
  );
}
