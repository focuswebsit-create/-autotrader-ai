// Response types of the read-only backend (src/autotrader/api/market_data.py).

export type Interval = "30m" | "1h";

export type CandleDto = {
  t: number; // open time, epoch seconds (UTC)
  open_time: string;
  close_time: string;
  o: string;
  h: string;
  l: string;
  c: string;
  v: string;
  n: number | null;
  closed: boolean;
  source: "rest" | "ws";
};

export type CandlesResponse = {
  coin: string;
  interval: Interval;
  as_of: string;
  count: number;
  truncated: boolean;
  candles: CandleDto[];
  forming: CandleDto | null;
};

export type Alert = {
  code: string;
  severity: "ERROR" | "WARNING";
  interval?: Interval;
  count?: number;
  missing_bars?: number;
  age_s?: number;
  error?: string | null;
  last_closed_open_time?: string | null;
};

export type IntervalStatus = {
  interval: Interval;
  closed_candles: number;
  first_open_time: string | null;
  last_closed_open_time: string | null;
  expected_last_closed_open_time: string;
  fresh: boolean;
  missing_bars: number;
  open_gaps: number;
  open_issues: Record<string, number>;
  forming_candle_open_time: string | null;
};

export type CollectorStatus = {
  service: string;
  service_state: "NOT_STARTED" | "STARTING" | "RUNNING" | "DEGRADED" | "STOPPED" | "DOWN";
  overall: "OK" | "WARNING" | "ERROR";
  heartbeat: null | {
    state: string;
    ws_state: "DISCONNECTED" | "CONNECTING" | "CONNECTED" | "STALE";
    started_at: string | null;
    last_heartbeat_at: string | null;
    heartbeat_age_s: number;
    last_ws_message_at: string | null;
    ws_message_age_s: number | null;
    last_candle_update_at: string | null;
    reconnects: number;
    messages: number | null;
    ignored_out_of_order: number | null;
    rejected: number | null;
    last_sync_at: string | null;
    last_sync_ok: boolean | null;
    last_error: string | null;
    last_error_at: string | null;
    recovered_error: string | null;
    recovered_at: string | null;
    market_ctx: { markPx?: string; oraclePx?: string; midPx?: string; funding?: string; received_at?: string };
  };
  intervals: IntervalStatus[];
  alerts: Alert[];
  checked_at: string;
};

export type Issue = {
  id: number;
  interval: Interval;
  type: string;
  severity: "INFO" | "WARNING" | "ERROR";
  open_time: string | null;
  range_end: string | null;
  missing_bars: number | null;
  detail: string;
  occurrences: number;
  first_detected_at: string | null;
  last_detected_at: string | null;
  resolved_at: string | null;
};

export type IngestionRunDto = {
  id: number;
  interval: Interval;
  kind: string;
  status: "RUNNING" | "OK" | "FAILED";
  received: number;
  inserted: number;
  updated: number;
  rejected: number;
  first_open_time: string | null;
  last_open_time: string | null;
  started_at: string | null;
  error: string | null;
};

export type DataReport = {
  coin: string;
  generated_at: string;
  source: string;
  history_limit_note: string;
  intervals: {
    interval: Interval;
    closed_candles: number;
    first_open_time: string | null;
    last_open_time: string | null;
    span_days: number;
    expected_bars_in_span: number;
    missing_bars: number;
    open_gaps: number;
    open_issues: Record<string, number>;
    forming_candle_open_time: string | null;
  }[];
  open_issues: unknown[];
  recent_ingestion_runs: IngestionRunDto[];
};

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };
