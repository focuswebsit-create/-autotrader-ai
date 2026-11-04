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
  PHASE_0_NO_STRATEGY: "שלב 0: עדיין אין אסטרטגיה ואין כניסות לעסקאות (גם לא וירטואליות).",
};

export function explain(code: string | null | undefined): string {
  if (!code) return "";
  return ERRORS[code] ?? `שגיאה (${code})`;
}
