import type { Metadata } from "next";

import Terminal from "./Terminal";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "טרמינל PAPER · AutoTrader AI",
  description: "טרמינל מסחר וירטואלי (PAPER) על xyz:CL. כסף מדומה בלבד.",
  robots: { index: false, follow: false },
};

export default function TerminalPage() {
  return <Terminal />;
}
