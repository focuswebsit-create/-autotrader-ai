// Hebrew labels and formatting for backtest reports. Values always come from the stored report.
import type { Verdict } from "./types";

export const VERDICT: Record<Verdict, { label: string; tone: "ok" | "warn" | "bad"; explain: string }> = {
  PASS: { label: "PASS", tone: "ok", explain: "כל קריטריוני ההוכחה התקיימו (אחרי עלויות, מחוץ למדגם, במבחני לחץ)." },
  FAIL: {
    label: "FAIL",
    tone: "bad",
    explain: "התוחלת אחרי עלויות שלילית באופן מובהק סטטיסטית (רווח סמך 95% כולו מתחת לאפס) בנתונים הקיימים.",
  },
  INCONCLUSIVE: {
    label: "INCONCLUSIVE",
    tone: "warn",
    explain: "אין מספיק נתונים או עסקאות כדי להוכיח יתרון סטטיסטי (או להפריך אותו). אין להסיק רווחיות.",
  },
  INVALID: { label: "INVALID", tone: "bad", explain: "הבדיקה העצמאית מצאה הפרות. אסור להשתמש בתוצאה." },
};

export const EXIT_REASON: Record<string, string> = {
  STOP: "סטופ",
  STOP_GAP: "סטופ (פער פתיחה)",
  TAKE_PROFIT: "יעד",
  TAKE_PROFIT_GAP: "יעד (פער פתיחה)",
  TIME: "יציאת זמן",
  END_OF_DATA: "סוף הנתונים/החלון",
};

export const BLOCK_REASON: Record<string, string> = {
  FILLED: "בוצע",
  REFERENCE_SESSION_CLOSED: "מסנן: CME סגורה (xyz:CL עצמו סחיר)",
  REFERENCE_SESSION_EDGE: "מסנן: קרוב לפתיחה/סגירה של CME",
  REFERENCE_HOLIDAY: "מסנן: חג ב־CME",
  CONTRACT_ROLL_WINDOW: "מסנן: חלון גלגול חוזה CL",
  SESSION_CALENDAR_UNKNOWN: "מסנן: לוח CME לא ידוע (חסימה שמרנית)",
  DAILY_LOSS_LIMIT: "מגבלת הפסד יומית",
  CONSECUTIVE_LOSSES_COOLDOWN: "השהיה אחרי 3 הפסדים רצופים",
  MAX_ENTRIES_PER_DAY: "מקסימום כניסות ביום",
  DRAWDOWN_HALT: "עצירה: ירידה של 5% מהשיא",
  BANKRUPT: "עצירה: הון אפס",
  ABNORMAL_VOLATILITY: "תנודתיות חריגה בנר האות",
  ABNORMAL_FUNDING: "Funding חריג",
  NO_NEXT_BAR: "אין נר הבא לביצוע",
  DATA_GAP: "פער בנתונים",
  NO_PLAUSIBLE_EXECUTION: "אין מסחר בנר האות",
  FUNDING_DATA_MISSING: "חסרים נתוני Funding (חסימה שמרנית)",
  SIZE_BELOW_MINIMUM: "גודל פוזיציה מתחת למינימום",
  INVALID_STOP: "סטופ לא תקין",
  SKIPPED_FILL_SIMULATED: "דילוג מדומה על ביצוע",
};

export const SEGMENT: Record<string, string> = {
  development: "פיתוח (70%)",
  validation: "אימות (15%)",
  holdout: "בדיקה סופית (15%)",
};

export const CRITERION: Record<string, string> = {
  zero_audit_violations: "אפס הפרות בביקורת העצמאית",
  min_trades: "מספר עסקאות מינימלי",
  expectancy_ci_above_zero: "רווח סמך 95% של התוחלת מעל 0",
  holdout_profitable: "רווחי בנתוני הבדיקה הסופית",
  walk_forward_oos_positive: "Walk-Forward מחוץ למדגם חיובי",
  survives_2x_costs: "רווחי גם בעלויות כפולות",
  history_covers_regimes: "היסטוריה מספיקה (≥180 יום)",
  holdout_untouched: "Holdout שלא נחשף קודם",
};

export function num(s: string | number | null | undefined, d = 2): string {
  if (s == null || s === "") return "—";
  const n = typeof s === "number" ? s : Number(s);
  if (!Number.isFinite(n)) return String(s);
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function signed(s: string | number | null | undefined, d = 2): string {
  if (s == null) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return String(s);
  return `${n > 0 ? "+" : ""}${num(n, d)}`;
}

export function pct(x: number | null | undefined, d = 1): string {
  return x == null ? "—" : `${(x * 100).toFixed(d)}%`;
}

export function tone(s: string | number | null | undefined): string {
  const n = Number(s);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "ok" : "err";
}

export function reason(code: string): string {
  const key = code.startsWith("BLOCKED:") ? code.slice(8) : code;
  return BLOCK_REASON[key] ?? key;
}

export const CATEGORY: Record<string, string> = {
  STRATEGY_FILTER: "מסנן אסטרטגי",
  RISK: "ניהול סיכונים",
  DATA: "נתונים",
  MARKET: "שוק (אין מסחר)",
  SIMULATION: "סימולציה",
  OTHER: "אחר",
};

export const PROVENANCE: Record<string, string> = {
  VERIFIED: "מאומת",
  OBSERVED: "נמדד בפועל",
  ASSUMPTION: "הנחה",
  UNVERIFIED: "לא מאומת",
};
