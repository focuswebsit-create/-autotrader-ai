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
  stale_forming_open_time: string | null;
  fresh: boolean;
  last_closed_open_time: string | null;
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

// ------------------------------------------------------------------ Phase 2: backtests
export type Verdict = "PASS" | "FAIL" | "INCONCLUSIVE" | "INVALID";

export type RunHeader = {
  id: number;
  created_at: string;
  coin: string;
  strategy_id: string;
  strategy_version: string;
  title_he: string | null;
  params_hash: string;
  code_version: string;
  data_fingerprint: string;
  data_start: string;
  data_end: string;
  verdict: Verdict;
  trades: number | null;
  net_pnl: string | null;
  win_rate: number | null;
  profit_factor: number | string | null;
  max_drawdown_pct: string | null;
  expectancy_r: number | null;
};

export type BtTrade = {
  id: number;
  side: "LONG" | "SHORT";
  signal_bar_open: string;
  signal_time: string;
  entry_time: string;
  entry_ref_price: string;
  entry_price: string;
  size: string;
  notional: string;
  stop: string;
  target: string;
  atr: string;
  risk_budget: string;
  planned_loss: string;
  equity_at_entry: string;
  exit_time: string;
  exit_bar_open?: string;
  exit_ref_price: string;
  exit_price: string;
  exit_reason: string;
  ambiguous_bar: boolean;
  entry_offhours: boolean;
  exit_offhours: boolean;
  crossed_data_gap: boolean;
  bars_held: number;
  gross_pnl: string;
  fees: string;
  spread_cost: string;
  slippage_cost: string;
  funding: string;
  funding_hours: number;
  funding_missing_hours: number;
  net_pnl: string;
  r_multiple: string;
  mae: string;
  mfe: string;
  rationale: Record<string, string>;
};

export type Summary = {
  trades: number;
  longs: number;
  shorts: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  profit_factor: number | string | null;
  net_pnl: string;
  gross_pnl_before_costs: string;
  fees: string;
  spread_cost: string;
  slippage_cost: string;
  funding_paid: string;
  total_costs: string;
  net_return_pct: string;
  final_equity: string;
  max_drawdown: string;
  max_drawdown_pct: string;
  avg_win: string | null;
  avg_loss: string | null;
  expectancy: string | null;
  expectancy_r: number | null;
  expectancy_r_ci95: [number, number] | null;
  t_stat_r: number | null;
  longest_losing_streak: number;
  time_in_market_pct: number;
  fee_drag_pct_of_gross: number | null;
  ambiguous_bars: number;
  exit_reasons: Record<string, number>;
  days: number;
  sharpe_daily_annualized: number | null;
  sortino_daily_annualized: number | null;
  ratios_note: string | null;
};

export type Compact = {
  trades: number;
  win_rate: number | null;
  profit_factor: number | string | null;
  net_pnl: string;
  net_return_pct: string;
  expectancy_r: number | null;
  expectancy_r_ci95: [number, number] | null;
  max_drawdown_pct: string;
  total_costs: string;
  funding_paid: string;
  days: number;
  violations: number;
};

export type Assumption = { item: string; value: string; provenance: "VERIFIED" | "OBSERVED" | "ASSUMPTION" | "UNVERIFIED"; note: string };

export type Report = {
  schema: string;
  paper_only: true;
  strategy: { id: string; version: string; title_he: string; description_he: string };
  params: Record<string, string | number | null>;
  params_hash: string;
  data: {
    coin: string;
    source: string;
    as_of: string;
    m30: { bars: number; first_open: string; last_close: string; gaps: number; missing_bars: number };
    h1: { bars: number; first_open: string; last_close: string; gaps: number; missing_bars: number };
    funding: { records: number; first: string | null; last: string | null; provenance: string };
    history_days_30m: number;
    market_hours?: {
      bars: number;
      bars_with_trades: number;
      min_trades_per_bar: number | null;
      bars_while_cme_closed: number;
      median_volume_cme_open: string | null;
      median_volume_cme_closed: string | null;
      conclusion: string;
    };
    fingerprint_sha256: string;
    limitation: string;
  };
  config: Record<string, Record<string, unknown>>;
  assumptions: Assumption[];
  trade_window: { start: string; end: string };
  primary: {
    summary: Summary;
    trades: BtTrade[];
    equity: [string, string, string][];
    decision_counts: Record<string, number>;
    blocked_by_category?: Record<string, number>;
    signals: { time: string; side: string; status: string; reason: string; trade_id: number | null }[];
    events: { time: string; event: string; detail: string }[];
    halted: string | null;
  };
  segments: (Compact & { name: string; start: string; end: string })[];
  optimization: {
    objective: string;
    grid_size: number;
    selected_params: Record<string, string | number | null>;
    selected_params_hash: string;
    segments: (Compact & { name: string; start: string; end: string })[];
    note: string;
  };
  independent_test?: {
    holdout_untouched: boolean;
    holdout_status: "UNTOUCHED" | "EXPOSED";
    exposed_until: string;
    fresh_bars_after_exposure: number;
    fresh_days_after_exposure: number;
    status: "AVAILABLE" | "NONE";
    note: string;
  };
  walk_forward: {
    train_days: number;
    test_days: number;
    objective: string;
    grid_size: number;
    folds: { train_start: string; train_end: string; test_start: string; test_end: string; params: Record<string, unknown>; params_hash: string; test: Compact }[];
    oos: { trades: number; win_rate: number | null; profit_factor: number | string | null; net_pnl: string; expectancy_r: number | null; expectancy_r_ci95: [number, number] | null };
    violations: number;
  };
  sensitivity: (Compact & { scenario: string; label_he: string })[];
  robustness: (Compact & { param: string; value: string })[];
  verdict: { status: Verdict; reasons: string[]; criteria: { criterion: string; passed: boolean; detail: string }[] };
  audit: { violations: string[]; violation_count: number };
  code: { commit: string; engine_version: string };
  report_sha256: string;
  generated_at?: string;
};

export type RunDetail = { header: RunHeader; report: Report };

// ------------------------------------------------------------------ Phase 2.5: forward tests
export type ForwardState = "ACTIVE" | "STOPPED" | "INTEGRITY_HALT";

export type ForwardTestHeader = {
  id: number;
  strategy_id: string;
  title_he: string | null;
  strategy_version: string;
  params_hash: string;
  state: ForwardState;
  starts_at: string;
  created_at: string;
};

export type ForwardDecision = {
  bar_open_time: string;
  bar_close_time: string;
  evaluated_at: string;
  late: boolean;
  side: "LONG" | "SHORT" | null;
  reason: string;
  atr: string | null;
  rationale: Record<string, string>;
  market: {
    bar: { open: string; high: string; low: string; close: string; volume: string; trades: number | null };
    funding_rate_last: string | null;
    mark_px: string | null;
    oracle_px: string | null;
    oracle_mark_divergence_bps: string | null;
  };
  strategy_version: string;
  params_hash: string;
  input_digest: string;
};

export type ForwardReport = {
  schema: string;
  paper_only: true;
  test: {
    id: number;
    state: ForwardState;
    strategy: { id: string; version: string; title_he: string };
    params: Record<string, string | number | null>;
    params_hash: string;
    code_hash: string;
    code_hash_matches: boolean;
    code_version: string;
    config_sha256: string;
    created_at: string;
    starts_at: string;
    history_anchor: string;
    thresholds: { min_trades: number; min_days: number; min_trade_days: number };
    costs: Record<string, string>;
    risk: Record<string, string>;
  };
  progress: {
    now: string;
    observed_days: number;
    decisions: number;
    signals: number;
    late_decisions: number;
    last_decision_bar: string | null;
    last_closed_bar: string | null;
    data_stale: boolean;
    closed_trades: number;
    trade_days: number;
  };
  summary: Summary | null;
  stats: { trades: number; win_rate: number | null; profit_factor: number | string | null; expectancy_r: number | null; expectancy_r_ci95: [number, number] | null; avg_cost_per_trade: string | null };
  open_position: { side: string; entry_time: string; entry_price: string; size: string; stop: string; target: string; unrealized_pnl: string; fees_paid: string; funding_paid: string } | null;
  pending_signal: { time: string; side: string } | null;
  trades: BtTrade[];
  equity: [string, string, string][];
  signals: ForwardDecision[];
  engine_signals: { time: string; side: string; status: string; reason: string; category: string; trade_id: number | null }[];
  events: { time: string; event: string; detail: Record<string, unknown> }[];
  parity: { checked: number; mismatches: number; examples: unknown[] };
  comparison: {
    available: boolean;
    note: string;
    reference_run_id?: number;
    reference_verdict?: string;
    reference_period?: [string, string];
    metrics?: { metric: string; backtest: number | string | null; forward: number | string | null }[];
    expectancy_consistency?: "INSUFFICIENT_DATA" | "CONSISTENT" | "FORWARD_WORSE" | "FORWARD_BETTER";
  };
  stress_2x_costs_net: string | null;
  audit: { violations: string[]; violation_count: number };
  verdict: { status: Verdict; reasons: string[]; criteria: { criterion: string; passed: boolean; detail: string }[] };
};
