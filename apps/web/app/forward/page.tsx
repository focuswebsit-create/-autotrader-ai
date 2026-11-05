import Link from "next/link";

import ForwardLab from "./ForwardLab";

export const dynamic = "force-dynamic";

export const metadata = { title: "Forward Testing · AutoTrader AI · PAPER" };

export default function ForwardPage() {
  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold">Forward Testing</h1>
          <p className="text-sm muted">
            שלב 2.5 · האסטרטגיות הוקפאו מראש ונבדקות על נתוני <bdi dir="ltr">xyz:CL</bdi> חדשים, נר אחרי נר, בזמן אמת.
            סימולציה בלבד — אין שליחת פקודות ואין כסף אמיתי.
          </p>
        </div>
        <nav className="flex gap-4 text-sm">
          <Link href="/terminal" className="underline">
            טרמינל PAPER
          </Link>
          <Link href="/backtest" className="underline">
            מעבדת Backtest
          </Link>
          <Link href="/" className="underline">
            נתוני שוק
          </Link>
        </nav>
      </header>
      <ForwardLab />
      <footer className="pb-6 text-center text-xs muted">
        רווח והפסד וירטואליים בלבד. תוצאות אינן המלצה למסחר. רענון אוטומטי כל דקה.
      </footer>
    </main>
  );
}
