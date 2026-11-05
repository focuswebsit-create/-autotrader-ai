// Same-origin, GET-only proxy from the browser to the read-only backend API.
// Only allow-listed paths are forwarded; nothing else is reachable through it.
import { NextRequest, NextResponse } from "next/server";

const BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

const ALLOWED = new Set([
  "candles",
  "collector/status",
  "data/report",
  "data/issues",
  "market/discovery",
  "system/mode",
  "backtest/runs",
  "backtest/strategies",
  "forward/tests",
  // Phase 3: PAPER trading terminal (read-only ledger views)
  "paper/summary",
  "paper/trades",
  "paper/decisions",
  "paper/events",
  "paper/equity",
  "paper/health",
]);
// Run details: backtest/runs/<numeric id> only.
const RUN_DETAIL = /^(backtest\/runs\/[1-9][0-9]{0,15}|forward\/tests\/[1-9][0-9]{0,15}(\/decisions)?)$/;

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const joined = path.join("/");
  if (!ALLOWED.has(joined) && !RUN_DETAIL.test(joined)) {
    return NextResponse.json({ detail: "NOT_ALLOWED" }, { status: 404 });
  }
  const target = `${BASE}/api/${joined}${req.nextUrl.search}`;
  try {
    const res = await fetch(target, {
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
  } catch {
    return NextResponse.json({ detail: "BACKEND_UNREACHABLE" }, { status: 502 });
  }
}
