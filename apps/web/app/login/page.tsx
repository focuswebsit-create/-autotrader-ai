import type { Metadata } from "next";

import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "התחברות · AutoTrader AI", robots: { index: false, follow: false } };

export default function LoginPage() {
  return <LoginForm />;
}
