import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoTrader AI · PAPER",
  description: "מערכת מחקר ומסחר וירטואלי בלבד (Paper Trading) על xyz:CL",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="he" dir="rtl">
      <body className="min-h-dvh antialiased">
        <div
          role="status"
          className="sticky top-0 z-10 border-b px-4 py-2 text-center text-sm font-bold"
          style={{ background: "var(--paper-bg)", color: "var(--paper)", borderColor: "var(--paper)" }}
        >
          PAPER / ללא כסף אמיתי — מסחר וירטואלי בלבד
        </div>
        {children}
      </body>
    </html>
  );
}
