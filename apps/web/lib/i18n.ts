// Hebrew explanations for machine-readable error codes. Technical details stay in backend logs.
const ERRORS: Record<string, string> = {
  BACKEND_UNREACHABLE: "לא ניתן להתחבר לשרת המערכת. ודא שה־API פועל.",
  DATABASE_NOT_CONFIGURED: "מסד הנתונים לא הוגדר בשרת.",
  DATABASE_UNAVAILABLE: "מסד הנתונים אינו זמין כרגע.",
  NO_DISCOVERY_YET: "עדיין לא בוצע איתור שוק. הפעל את ה־worker.",
  API_UNAVAILABLE: "ה־API הציבורי של Hyperliquid אינו זמין כרגע.",
  DEX_NOT_FOUND: "ה־dex של השוק לא נמצא ב־Hyperliquid.",
  DEX_AMBIGUOUS: "ה־dex מופיע יותר מפעם אחת. האיתור נעצר מטעמי בטיחות.",
  MARKET_NOT_FOUND: "השוק לא נמצא ב־Hyperliquid.",
  MARKET_AMBIGUOUS: "השוק מופיע יותר מפעם אחת. האיתור נעצר מטעמי בטיחות.",
  MARKET_DELISTED: "השוק הוסר מהמסחר.",
  MALFORMED_RESPONSE: "התקבלה תשובה לא תקינה מ־Hyperliquid.",
  INVALID_PRICES: "התקבלו מחירים לא תקינים.",
  OUT_OF_RANGE: "התקבלו ערכים חריגים ולא סבירים. הנתונים נדחו מטעמי בטיחות.",
  INVALID_TARGET: "הגדרת השוק בשרת אינה תקינה.",
  DB_REJECTED: "מסד הנתונים דחה את הנתונים שהתקבלו.",
  UNEXPECTED_ERROR: "אירעה שגיאה לא צפויה באיתור השוק.",
  BACKEND_UNREACHABLE_PROXY: "השרת אינו זמין.",
  START_AFTER_END: "תאריך ההתחלה חייב להיות לפני תאריך הסיום.",
  NOT_ALLOWED: "הבקשה אינה מותרת.",
  PHASE_0_NO_STRATEGY: "עדיין אין אסטרטגיה במערכת, ולכן אין כניסות לעסקאות (גם לא וירטואליות).",
};

import type { Alert } from "./types";

const ALERTS: Record<string, string> = {
  COLLECTOR_NOT_STARTED: "שירות איסוף הנתונים לא הופעל מעולם.",
  COLLECTOR_STOPPED: "שירות איסוף הנתונים נעצר.",
  COLLECTOR_DOWN: "שירות איסוף הנתונים לא דיווח זמן רב. ייתכן שקרס.",
  WS_DISCONNECTED: "החיבור החי (WebSocket) ל־Hyperliquid מנותק. מתבצע ניסיון התחברות מחדש.",
  WS_STALE: "לא התקבלו עדכונים חיים מ־Hyperliquid לאחרונה.",
  REST_SYNC_FAILED: "הסנכרון האחרון מול ה־API של Hyperliquid נכשל.",
  NO_DATA: "אין נרות שמורים עבור טווח זמן זה.",
  DATA_STALE: "הנר הסגור האחרון ישן מהצפוי. ייתכן שחסרים עדכונים.",
  GAPS: "התגלו פערים בנתונים (נרות חסרים). הנתונים לא הושלמו באופן מלאכותי.",
  REJECTED_CANDLES: "נרות לא תקינים נדחו ותועדו.",
};

export function explainAlert(a: Alert): string {
  const base = ALERTS[a.code] ?? `התראה (${a.code})`;
  const parts: string[] = [];
  if (a.interval) parts.push(a.interval);
  if (a.count != null) parts.push(`${a.count}`);
  if (a.missing_bars != null) parts.push(`${a.missing_bars} נרות חסרים`);
  return parts.length ? `${base} (${parts.join(" · ")})` : base;
}

export const ISSUE_TYPES: Record<string, string> = {
  GAP: "פער (נרות חסרים)",
  DUPLICATE_CONFLICT: "כפילות עם ערכים סותרים",
  REJECTED_CANDLE: "נר לא תקין נדחה",
  OUTLIER_RETURN: "תנועת מחיר חריגה",
  OUTLIER_RANGE: "טווח נר חריג",
  REVISED_CLOSED_CANDLE: "שינוי בנר שכבר נסגר",
  OUT_OF_ORDER: "הודעה לא בסדר",
  STALE_DATA: "נתונים מיושנים",
};

export const SERVICE_STATES: Record<string, string> = {
  NOT_STARTED: "לא הופעל",
  STARTING: "עולה",
  RUNNING: "פועל",
  DEGRADED: "פועל חלקית",
  STOPPED: "עצור",
  DOWN: "לא מגיב",
};

export const WS_STATES: Record<string, string> = {
  CONNECTED: "מחובר",
  CONNECTING: "מתחבר",
  DISCONNECTED: "מנותק",
  STALE: "ללא עדכונים",
};

export function explain(code: string | null | undefined): string {
  if (!code) return "";
  return ERRORS[code] ?? `שגיאה (${code})`;
}
