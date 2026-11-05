// Phase 3 PAPER terminal: response types of /api/paper/* and Hebrew labels.
// Every number shown comes from the paper worker's ledger (never from backtests).

export type BotState = "STOPPED" | "STARTING" | "RUNNING" | "PAUSED" | "DEGRADED" | "HALTED";

export type Check = { check: string; ok: boolean; detail: unknown };

export type Decision = {
  bar_open_time: string;
  bar_close_time: string;
  evaluated_at: string;
  side: "LONG" | "SHORT" | null;
  reason: string;
  outcome: "ORDER" | "BLOCKED" | "NO_SIGNAL" | "IN_POSITION";
  block_reason: string | null;
  atr: string | null;
  rationale: Record<string, unknown> | null;
  market: Record<string, unknown> | null;
};

export type Position = {
  id: number;
  side: "LONG" | "SHORT";
  size: string;
  entry_price: string;
  stop: string;
  target: string;
  opened_at: string;
  exit_price_now: string | null;
  unrealized: string | null;
  unrealized_note: string;
  fees_paid: string;
  funding_paid: string;
  risk_budget: string;
  planned_loss: string;
  open_reason: string | null;
  signal_bar: string | null;
};

export type Summary = {
  paper_only: true;
  now: string;
  account: {
    name: string;
    strategy_id: string;
    strategy_version: string;
    params_hash: string;
    initial_equity: string;
    created_at: string;
    risk: Record<string, string | number> | null;
    experimental_warning: string;
  };
  bot: { state: BotState; reason: string; since: string; detail: Record<string, unknown> | null; session_start: string | null };
  market: {
    worker: string;
    heartbeat_age_s?: number;
    book?: { time: string; bid: string; ask: string; mid: string; spread_bps: string } | null;
    book_error?: string | null;
    fresh: boolean;
    market_ctx?: Record<string, string>;
    last_funding_sync?: string | null;
  };
  pnl: {
    cash: string;
    equity: string | null;
    unrealized: string | null;
    realized_net: string;
    gross_realized: string;
    fees: string;
    funding: string;
    total_pnl: string | null;
    total_pct: string | null;
    day_start_equity: string;
    today_pnl: string | null;
    today_realized: string;
    today_unrealized: string | null;
    day_basis: string;
  };
  stats: {
    closed_trades: number;
    open_positions: number;
    wins: number;
    losses: number;
    win_rate: number | null;
    profit_factor: number | "inf" | null;
    max_drawdown_pct: string;
  };
  position: Position | null;
  risk: {
    entries_today: number;
    max_entries_per_day: number;
    daily_loss_limit_hit: boolean;
    consecutive_losses: number;
    halt_after_consecutive_losses: number;
    high_water: string;
    drawdown_halt_pct: string;
    daily_loss_limit_pct: string;
    risk_per_trade_pct: string;
    max_leverage: string;
  };
  last_decision: Decision | null;
  readiness: { ok: boolean; fresh: boolean; reason: string | null; at?: string; checks: Check[] };
};

export type Trade = {
  id: number;
  status: "OPEN" | "CLOSED";
  side: "LONG" | "SHORT";
  strategy: string;
  opened_at: string;
  closed_at: string | null;
  entry_price: string;
  exit_price: string | null;
  size: string;
  stop: string;
  target: string;
  entry_fill: string | null;
  entry_touch: string | null;
  fees: string | null;
  funding: string | null;
  gross_pnl: string | null;
  net_pnl: string | null;
  open_reason: string | null;
  close_reason: string | null;
  signal_bar: string | null;
};

export type BotEvent = { time: string; type: "BOT" | "RISK"; kind: string; reason: string; detail: unknown };

export type EquityCurve = { points: [string, string, string][]; daily_pnl: [string, string][]; initial_equity: string };

export const STATE_LABEL: Record<BotState, { he: string; tone: "ok" | "warn" | "bad" | "muted" }> = {
  RUNNING: { he: "פועל", tone: "ok" },
  STARTING: { he: "מתחיל…", tone: "warn" },
  PAUSED: { he: "מושהה", tone: "warn" },
  DEGRADED: { he: "תקלה · ללא כניסות חדשות", tone: "bad" },
  HALTED: { he: "נעצר לבדיקה", tone: "bad" },
  STOPPED: { he: "כבוי", tone: "muted" },
};

const REASONS: Record<string, string> = {
  CREATED: "החשבון נוצר",
  START_REQUESTED: "התקבלה בקשת התחלה",
  STARTED: "הבוט הופעל לאחר שכל הבדיקות עברו",
  PREFLIGHT_FAILED: "ההפעלה נדחתה: בדיקת מוכנות נכשלה",
  RESUMED: "הבוט חודש",
  PAUSED_BY_OWNER: "הושהה על ידי הבעלים",
  STOPPED_BY_OWNER: "נעצר על ידי הבעלים",
  KILL_SWITCH: "Kill Switch הופעל: אין כניסות חדשות",
  CLOSE_AND_STOP: "הפוזיציה נסגרה והבוט נעצר",
  CLOSE_FAILED: "סגירת הפוזיציה נכשלה (אין ספר פקודות זמין)",
  DRAWDOWN_HALT: "מגבלת ירידה מקסימלית (5%) הופעלה",
  CONSECUTIVE_LOSSES: "3 הפסדים ברצף: נעצר לבדיקה",
  HALT_RESET: "העצירה אופסה על ידי הבעלים",
  RESTART_RECOVERY: "המערכת עלתה מחדש והבדיקות עברו",
  RESTART_CHECKS: "המערכת עלתה מחדש: בודקת לפני חידוש מסחר",
  RESTART_DEGRADED: "המערכת עלתה מחדש: הבדיקות נכשלו",
  RECOVERED: "הנתונים חזרו: הבוט חזר לפעול",
  DEGRADED: "הבוט במצב תקלה",
  DAILY_LOSS_LIMIT: "מגבלת הפסד יומית (1%) הופעלה",
  FUNDING_ASSUMED: "חסר נתון מימון: חויב שיעור שמרני",
  MAX_ENTRIES_PER_DAY: "הושג מספר הכניסות המקסימלי היום",
  ENTRY_BLOCKED: "כניסה נחסמה",
};

export const BLOCK_LABEL: Record<string, string> = {
  IN_POSITION: "כבר קיימת פוזיציה פתוחה",
  ENTRY_WINDOW_MISSED: "חלון הכניסה עבר (אין מסחר רטרואקטיבי)",
  REFERENCE_SESSION_CLOSED: "מחוץ לשעות הייחוס של CME (מסנן האסטרטגיה)",
  REFERENCE_SESSION_EDGE: "סמוך לפתיחת/סגירת סשן CME",
  REFERENCE_HOLIDAY: "חג בבורסת הייחוס (CME)",
  CONTRACT_ROLL_WINDOW: "חלון גלגול חוזה",
  SESSION_CALENDAR_UNKNOWN: "לוח השנה של הסשן לא ידוע",
  DAILY_LOSS_LIMIT: "מגבלת הפסד יומית",
  MAX_ENTRIES_PER_DAY: "מספר כניסות יומי מקסימלי",
  INVALID_ATR: "ATR לא תקין",
  ABNORMAL_VOLATILITY: "תנודתיות חריגה",
  ABNORMAL_FUNDING: "שיעור מימון חריג",
  FUNDING_DATA_MISSING: "חסרים נתוני מימון",
  ORDER_BOOK_UNAVAILABLE: "ספר הפקודות אינו זמין",
  ORDER_BOOK_STALE: "ספר הפקודות אינו עדכני",
  SPREAD_TOO_WIDE: "מרווח (spread) רחב מדי",
  INSUFFICIENT_DEPTH: "אין עומק מספיק בספר",
  SIZE_BELOW_MINIMUM: "הגודל קטן מהמינימום",
  INVALID_STOP: "סטופ לא תקין",
  ACCOUNT_INCONSISTENT: "החשבון אינו מתיישב עם היומן",
  STRATEGY_CODE_CHANGED: "קוד האסטרטגיה השתנה מאז ההקפאה",
  COLLECTOR_NOT_RUNNING: "שירות איסוף הנתונים אינו פועל",
  COLLECTOR_WS_DISCONNECTED: "החיבור החי מנותק",
  MARKET_DATA_STALE: "נתוני השוק אינם עדכניים",
};

export const CHECK_LABEL: Record<string, string> = {
  DATABASE: "מסד נתונים",
  COLLECTOR: "שירות איסוף נתונים חי",
  MARKET_DATA_FRESH: "נרות עדכניים",
  ORDER_BOOK: "ספר פקודות חי (L2)",
  FUNDING_DATA: "נתוני מימון",
  STRATEGY_FROZEN: "אסטרטגיה מוקפאת",
  RISK_LIMITS: "מגבלות סיכון פעילות",
  LEDGER: "יומן חשבונאי מתיישב",
  SINGLE_ENGINE: "מנוע יחיד פעיל",
  PAPER_ONLY: "PAPER בלבד",
  ENGINE: "מנוע המסחר הווירטואלי",
};

export const CLOSE_LABEL: Record<string, string> = {
  STOP: "סטופ",
  TAKE_PROFIT: "יעד רווח",
  MANUAL_CLOSE: "סגירה ידנית",
  KILL: "Kill Switch",
};

export const CONTROL_ERRORS: Record<string, string> = {
  NOT_AUTHENTICATED: "יש להתחבר כדי לשלוט בבוט.",
  ADMIN_REQUIRED: "רק משתמש מנהל (ADMIN) יכול לשלוט בבוט.",
  CSRF_INVALID: "אסימון האבטחה פג. רענן את הדף ונסה שוב.",
  ORIGIN_NOT_ALLOWED: "הבקשה נשלחה ממקור לא מורשה.",
  RATE_LIMITED: "יותר מדי פעולות בדקה. המתן מעט.",
  TOO_MANY_FAILED_LOGINS: "יותר מדי ניסיונות כושלים. נסה שוב בעוד 15 דקות.",
  INVALID_CREDENTIALS: "שם משתמש או סיסמה שגויים.",
  CONFIRMATION_REQUIRED: "נדרש אישור מפורש לפעולה זו.",
  COMMAND_PENDING: "פקודה קודמת עדיין ממתינה לביצוע.",
  IDEMPOTENCY_KEY_REUSED: "הבקשה כבר נשלחה עם פעולה אחרת.",
  DUPLICATE_COMMAND: "הפקודה כבר נשלחה.",
  ACCOUNT_NOT_FOUND: "חשבון ה־PAPER לא נמצא.",
  CONTROL_UNREACHABLE: "שירות השליטה אינו זמין.",
  DATABASE_UNAVAILABLE: "מסד הנתונים אינו זמין כרגע.",
  PAPER_ACCOUNT_NOT_FOUND: "עדיין לא נוצר חשבון PAPER (autotrader paper init).",
  BACKEND_UNREACHABLE: "לא ניתן להתחבר לשרת המערכת.",
};

export function blockHe(code: string | null | undefined): string {
  if (!code) return "—";
  if (code.startsWith("BOT_")) {
    const st = code.slice(4).split(":")[0] as BotState;
    return `הבוט אינו פועל (${STATE_LABEL[st]?.he ?? st})`;
  }
  return BLOCK_LABEL[code] ?? code;
}

export function reasonHe(code: string | null | undefined): string {
  if (!code) return "—";
  if (code.startsWith("REFUSED_")) return `הפקודה ${code.slice(8)} נדחתה`;
  return REASONS[code] ?? BLOCK_LABEL[code] ?? code;
}

export function controlError(code: string): string {
  if (code.startsWith("NOT_ALLOWED_FROM_")) {
    const st = code.slice("NOT_ALLOWED_FROM_".length) as BotState;
    return `הפעולה אינה זמינה כשהבוט במצב "${STATE_LABEL[st]?.he ?? st}".`;
  }
  return CONTROL_ERRORS[code] ?? `שגיאה (${code})`;
}

/** Signed money string with 2 decimals; null -> em dash. */
export function money(s: string | null | undefined, sign = true): string {
  if (s == null) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!sign) return `${n < 0 ? "-" : ""}$${abs}`;
  return `${n > 0 ? "+" : n < 0 ? "-" : ""}$${abs}`;
}

export function pct(s: string | number | null | undefined, digits = 2, sign = true): string {
  if (s == null) return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return "—";
  return `${sign && n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

export function tone(s: string | null | undefined): "pos" | "neg" | "flat" {
  const n = Number(s);
  if (s == null || !Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "pos" : "neg";
}
