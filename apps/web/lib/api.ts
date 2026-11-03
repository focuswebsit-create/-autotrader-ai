// Server-side helpers to read backend state. Values shown in the UI come from
// these calls only; nothing is mocked.

export type Health = {
  status: string;
  trading_mode: "PAPER";
  paper_only: true;
  database: "ok" | "unavailable" | "not_configured";
  version: string;
  time: string;
};

export type Discovery = {
  dex: string;
  coin: string;
  dex_index: number;
  asset_index_in_dex: number;
  sz_decimals: number;
  max_price_decimals: number;
  max_leverage: number;
  mark_px: string;
  oracle_px: string;
  mid_px: string | null;
  funding_rate: string;
  open_interest: string;
  oracle_mark_divergence_bps: string;
  status: "ACTIVE" | "DEGRADED";
  warnings: string[];
  discovered_at: string;
  source: string;
};

export type DiscoveryState = "CURRENT" | "STALE" | "FAILED";

export type DiscoveryResponse = {
  discovery: Discovery;
  state: DiscoveryState;
  age_seconds: number;
  stale: boolean;
  last_failure_after_discovery: { occurred_at: string; code: string; message: string } | null;
  entries_allowed: boolean;
  entries_blocked_reason: string;
};

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

const BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

async function getJson<T>(path: string, okStatuses: number[] = []): Promise<Result<T>> {
  try {
    const res = await fetch(`${BASE}${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
      redirect: "error",
    });
    if (!res.ok && !okStatuses.includes(res.status)) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = (await res.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch {
        // ignore non-JSON error bodies
      }
      return { ok: false, error: detail };
    }
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, error: "BACKEND_UNREACHABLE" };
  }
}

// /health answers 503 with a full body when degraded; that body is still the real state.
export const getHealth = () => getJson<Health>("/health", [503]);
export type SystemMode = {
  trading_mode: "PAPER";
  live_trading_available: false;
  market: { dex: string; coin: string };
};

export const getMode = () => getJson<SystemMode>("/api/system/mode");
export const getDiscovery = () => getJson<DiscoveryResponse>("/api/market/discovery");
