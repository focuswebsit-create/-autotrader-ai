"use client";
// Browser-side reads through the same-origin GET proxy (/api/backend/...).
import type { Result } from "./types";

export async function getBackend<T>(path: string, params?: Record<string, string>): Promise<Result<T>> {
  const qs = params ? `?${new URLSearchParams(params).toString()}` : "";
  try {
    const res = await fetch(`/api/backend/${path}${qs}`, { cache: "no-store" });
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
    return { ok: false, error: "BACKEND_UNREACHABLE" };
  }
}
