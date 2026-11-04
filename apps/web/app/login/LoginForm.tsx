"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { login } from "@/lib/control";
import { controlError } from "@/lib/paper";

function Form() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next");
  // only same-site relative targets (no open redirect)
  const target = next && /^\/(?![/\\])[a-z/]*$/.test(next) ? next : "/terminal";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await login(username.trim(), password);
    setBusy(false);
    if (r.ok) {
      setPassword("");
      router.replace(target);
    } else setError(controlError(r.error));
  };

  return (
    <div className="terminal flex min-h-[80dvh] items-center justify-center">
      <form onSubmit={(e) => void submit(e)} className="t-card w-full max-w-sm" aria-labelledby="login-title">
        <div className="mb-4 flex items-center gap-2">
          <span className="t-paper">PAPER TRADING</span>
          <h1 id="login-title" className="text-lg font-bold">
            התחברות לבעלים
          </h1>
        </div>
        <label className="mb-3 block text-sm">
          שם משתמש
          <input
            className="t-input mt-1"
            name="username"
            autoComplete="username"
            dir="ltr"
            required
            maxLength={64}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="mb-4 block text-sm">
          סיסמה
          <input
            className="t-input mt-1"
            name="password"
            type="password"
            autoComplete="current-password"
            dir="ltr"
            required
            maxLength={256}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button type="submit" className="t-btn-start" disabled={busy || !username || !password}>
          {busy ? "מתחבר…" : "התחבר"}
        </button>
        {error && (
          <p role="alert" className="mt-3 text-sm t-neg">
            {error}
          </p>
        )}
        <p className="mt-4 text-xs t-muted">
          השליטה בבוט הווירטואלי מוגבלת למשתמש מנהל. 5 ניסיונות כושלים נועלים ל־15 דקות. אין כאן שום חיבור לארנק או לכסף
          אמיתי.
        </p>
        <p className="mt-2 text-xs">
          <Link href="/terminal" className="t-link">
            לצפייה בטרמינל בלי להתחבר
          </Link>
        </p>
      </form>
    </div>
  );
}

export default function LoginForm() {
  return (
    <Suspense>
      <Form />
    </Suspense>
  );
}
