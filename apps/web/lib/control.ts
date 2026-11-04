"use client";
// Browser-side calls to the authenticated Control API (through /api/control/...).
import type { Result } from "./types";

export type Me = { user: string; role: "ADMIN" | "VIEWER"; csrf_token: string };
export type BotAction = "start" | "stop" | "pause" | "resume" | "kill" | "close-and-stop" | "reset-halt";
export type CommandResult = { command_id: number; command: string; duplicate: boolean; state_before?: string };

async function call<T>(path: string, init: RequestInit): Promise<Result<T>> {
  try {
    const res = await fetch(`/api/control/${path}`, { cache: "no-store", credentials: "same-origin", ...init });
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      const detail = (body as { detail?: unknown } | null)?.detail;
      return { ok: false, error: typeof detail === "string" ? detail : `HTTP_${res.status}` };
    }
    return { ok: true, data: body as T };
  } catch {
    return { ok: false, error: "CONTROL_UNREACHABLE" };
  }
}

export function getMe(): Promise<Result<Me>> {
  return call<Me>("me", { method: "GET" });
}

export function login(username: string, password: string): Promise<Result<Me>> {
  return call<Me>("login", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function logout(csrf: string): Promise<Result<{ ok: boolean }>> {
  return call("logout", { method: "POST", headers: { "content-type": "application/json", "x-csrf-token": csrf }, body: "{}" });
}

/** One idempotency key per user intent: a double click or a retry sends the same key. */
export function newKey(action: BotAction): string {
  const rnd = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
  return `${action}-${rnd}`.slice(0, 64);
}

export function botCommand(
  action: BotAction,
  csrf: string,
  key: string,
  opts: { reason?: string; confirm?: boolean } = {},
): Promise<Result<CommandResult>> {
  return call<CommandResult>(`bot/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify({ idempotency_key: key, reason: opts.reason ?? "", confirm: opts.confirm ?? false }),
  });
}
