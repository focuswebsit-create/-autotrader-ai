// Same-origin proxy from the browser to the Control API (a separate service that is
// never published on the host). Only allow-listed paths and methods are forwarded.
//
// The session cookie is HttpOnly + SameSite=Strict and the Control API itself checks
// the CSRF token, the Origin header and the ADMIN role on every state change. This
// proxy adds nothing to that trust: it forwards the cookie, the CSRF header, the
// browser's Origin and the JSON body, and passes Set-Cookie back. It never trades and
// cannot reach any exchange: the Control API only appends PAPER bot commands.
import { NextRequest, NextResponse } from "next/server";

const BASE = process.env.CONTROL_BASE_URL ?? "http://localhost:8001";
const COOKIE = "at_session";
const MAX_BODY = 4096;

const GET_PATHS = new Set(["me", "health"]);
const POST_PATHS = new Set(["login", "logout"]);
const BOT = /^bot\/(start|stop|pause|resume|kill|close-and-stop|reset-halt)$/;

export const dynamic = "force-dynamic";

function sessionCookie(req: NextRequest): string | null {
  const v = req.cookies.get(COOKIE)?.value;
  return v && /^[A-Za-z0-9_-]{20,200}$/.test(v) ? `${COOKIE}=${v}` : null;
}

async function forward(req: NextRequest, joined: string, method: "GET" | "POST"): Promise<NextResponse> {
  const headers: Record<string, string> = { accept: "application/json" };
  const cookie = sessionCookie(req);
  if (cookie) headers.cookie = cookie;
  const origin = req.headers.get("origin");
  if (origin) headers.origin = origin;
  const csrf = req.headers.get("x-csrf-token");
  if (csrf && /^[0-9a-f]{64}$/.test(csrf)) headers["x-csrf-token"] = csrf;
  const xff = req.headers.get("x-forwarded-for");
  if (xff) headers["x-forwarded-for"] = xff.split(",")[0].trim().slice(0, 64);
  let body: string | undefined;
  if (method === "POST") {
    if (!(req.headers.get("content-type") ?? "").startsWith("application/json")) {
      return NextResponse.json({ detail: "JSON_REQUIRED" }, { status: 415 });
    }
    body = await req.text();
    if (body.length > MAX_BODY) return NextResponse.json({ detail: "BODY_TOO_LARGE" }, { status: 413 });
    headers["content-type"] = "application/json";
  }
  try {
    const res = await fetch(`${BASE}/control/${joined}`, {
      method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    const out = new NextResponse(await res.text(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
    for (const c of res.headers.getSetCookie()) {
      if (c.startsWith(`${COOKIE}=`)) out.headers.append("set-cookie", c);
    }
    return out;
  } catch {
    return NextResponse.json({ detail: "CONTROL_UNREACHABLE" }, { status: 502 });
  }
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const joined = (await ctx.params).path.join("/");
  if (!GET_PATHS.has(joined)) return NextResponse.json({ detail: "NOT_ALLOWED" }, { status: 404 });
  return forward(req, joined, "GET");
}

export async function POST(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const joined = (await ctx.params).path.join("/");
  if (!POST_PATHS.has(joined) && !BOT.test(joined)) {
    return NextResponse.json({ detail: "NOT_ALLOWED" }, { status: 404 });
  }
  return forward(req, joined, "POST");
}
