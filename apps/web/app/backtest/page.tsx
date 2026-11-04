import Link from "next/link";

import BacktestLab from "./BacktestLab";

export const dynamic = "force-dynamic";

export const metadata = { title: "מעבדת Backtest · AutoTrader AI · PAPER" };

export default function BacktestPage() {
  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold">מעבדת Backtest</h1>
          <p className="text-sm muted">
            שלב 2 · בדיקת אסטרטגיות על נתוני <bdi dir="ltr">xyz:CL</bdi> אמיתיים מ־Hyperliquid · נרות סגורים בלבד ·
            כניסה בפתיחת הנר הבא · כל העלויות כלולות. סימולציה בלבד — אין שליחת פקודות.
          </p>
        </div>
        <nav className="text-sm">
          <Link href="/" className="underline">
            ← לוח נתוני שוק
          </Link>
        </nav>
      </header>
      <BacktestLab />
      <footer className="pb-6 text-center text-xs muted">
        תוצאות עבר אינן מבטיחות תוצאות עתידיות. זהו מחקר בלבד ואינו המלצה לקנייה או מכירה. כל הנתונים מוצגים כפי שנשמרו
        בהרצה (בלתי ניתנת לשינוי).
      </footer>
    </main>
  );
}
