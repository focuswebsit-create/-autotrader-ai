// Hebrew explanations for machine-readable error codes. Technical details stay in backend logs.
const ERRORS: Record<string, string> = {
  BACKEND_UNREACHABLE: "לא ניתן להתחבר לשרת המערכת. ודא שה־API פועל.",
  DATABASE_NOT_CONFIGURED: "מסד הנתונים לא הוגדר בשרת.",
  DATABASE_UNAVAILABLE: "מסד הנתונים אינו זמין כרגע.",
  NO_DISCOVERY_YET: "עדיין לא בוצע איתור שוק. הפעל את ה־worker.",
  API_UNAVAILABLE: "ה־API הציבורי של Hyperliquid אינו זמין כרגע.",
  DEX_NOT_FOUND: "ה־dex ‏xyz לא נמצא ב־Hyperliquid.",
  MARKET_NOT_FOUND: "השוק xyz:CL לא נמצא.",
  MARKET_DELISTED: "השוק xyz:CL הוסר מהמסחר.",
  MALFORMED_RESPONSE: "התקבלה תשובה לא תקינה מ־Hyperliquid.",
  INVALID_PRICES: "התקבלו מחירים לא תקינים.",
  PHASE_0_NO_STRATEGY: "שלב 0: עדיין אין אסטרטגיה ואין כניסות לעסקאות (גם לא וירטואליות).",
};

export function explain(code: string | null | undefined): string {
  if (!code) return "";
  return ERRORS[code] ?? `שגיאה (${code})`;
}
