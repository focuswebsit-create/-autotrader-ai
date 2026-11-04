// All times are shown in UTC (the system's reference clock).

export function fmtUtc(iso: string | null | undefined, withSeconds = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const s = d.toISOString().replace("T", " ");
  return withSeconds ? s.slice(0, 19) : s.slice(0, 16);
}

export function fmtDate(iso: string | null | undefined): string {
  return iso ? fmtUtc(iso).slice(0, 10) : "—";
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))} שנ׳`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} דק׳`;
  if (seconds < 48 * 3600) return `${Math.round(seconds / 3600)} שע׳`;
  return `${Math.round(seconds / 86400)} ימים`;
}

export function ageFrom(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (now - t) / 1000;
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("en-US");
}

/** Shorten a decimal string for display without rounding it numerically. */
export function fmtNum(s: string | null | undefined, maxDecimals = 6): string {
  if (!s) return "—";
  const [i, f = ""] = s.split(".");
  const frac = f.slice(0, maxDecimals).replace(/0+$/, "");
  return frac ? `${i}.${frac}` : i;
}
