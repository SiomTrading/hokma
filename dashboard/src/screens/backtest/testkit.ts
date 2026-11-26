import type {
  HeatmapResponse,
  MonteCarloData,
  QuestionState,
  RunSummary,
  StrategyRow,
  ThresholdSet,
  Verdict,
  VerdictCheck,
  WalkForwardData,
} from "./types";

/** Answers a server would give. They are fixtures of the screen tests: the screen shows them as they are. */

const CHECKLIST: [number, string, VerdictCheck["phase"]][] = [
  [1, "point_in_time", "data_integrity"],
  [2, "survivorship_free", "data_integrity"],
  [3, "corporate_actions", "data_integrity"],
  [4, "no_leakage", "data_integrity"],
  [5, "data_gaps", "data_integrity"],
  [6, "trials_counted", "specification"],
  [7, "in_sample_tuning", "specification"],
  [8, "holdout_untouched", "specification"],
  [9, "history_length", "specification"],
  [10, "costs_charged", "execution"],
  [11, "impact_modeled", "execution"],
  [12, "financing_and_borrow", "execution"],
  [13, "house_fill", "execution"],
  [14, "capacity", "execution"],
  [15, "validation_scheme", "statistics"],
  [16, "multiple_testing", "statistics"],
  [17, "chance_hurdle", "statistics"],
  [18, "deflated_sharpe", "statistics"],
  [19, "pbo", "overfitting"],
  [20, "parameter_neighbors", "overfitting"],
  [21, "regimes", "overfitting"],
  [22, "red_flags", "overfitting"],
  [23, "gross_and_net", "reporting"],
  [24, "drawdown_analysis", "reporting"],
];

export function makeChecks(overrides: Record<string, Partial<VerdictCheck>> = {}): VerdictCheck[] {
  return CHECKLIST.map(([number, key, phase]) => ({
    number,
    key,
    phase,
    status: "pass" as const,
    note: `${key} ok`,
    excluded: false,
    ...overrides[key],
  }));
}

export function makeVerdict(overrides: Partial<Verdict> = {}): Verdict {
  return {
    verdict_id: 41,
    strategy_id: "ema_cross_nq",
    strategy_version: 3,
    line: "DT",
    verdict: "no_go",
    audit_verdict: "no_go",
    advisory: true,
    sentences: [
      "Suggestion: do not trade yet. 2 checks fail; the main one: probability of overfitting (0.58, the red limit is 0.50).",
      "Most solid: the deflated Sharpe is 0.98 (minimum 0.95).",
      "Suggested next step: remove a parameter or lengthen the history.",
    ],
    checks: makeChecks({
      costs_charged: { status: "fail", note: "cost_share: costs take 41% of the gross Sharpe; the limit is 33%" },
      pbo: { status: "fail", note: "PBO 0.580 is at or above 0.5" },
      parameter_neighbors: { status: "warn", note: "edge: best point on the border of: slow; worst neighbor drops 12%" },
      survivorship_free: { status: "unknown", note: "no evidence was given" },
      regimes: { status: "na", note: "the strategy does not use market regimes" },
    }),
    excluded: [],
    open_questions: [{ key: "survivorship_free", scope: "data", subject: "CME:NQ" }],
    suggestions: [
      { check: 10, key: "costs_charged", status: "fail", action: "reduce_turnover", text: "Trade less or with a wider target.", params: { pct: 0.41, max: 0.33 } },
      { check: 19, key: "pbo", status: "fail", action: "reduce_overfit", text: "Fewer parameters or shorter ranges; more history.", params: { pbo: 0.58, limit: 0.5 } },
      { check: 20, key: "parameter_neighbors", status: "warn", action: "widen_range", text: "Widen the range of slow beyond 400.", params: { params: "slow", value: 400 } },
      { check: 2, key: "survivorship_free", status: "unknown", action: "answer_question", text: "Answer the question on the screen.", params: { question: "survivorship_free" } },
    ],
    ideal: { status: "found", params: { fast: 12, slow: 160 }, plateau_size: 14, plateau_mean: 1.18, run_id: 845, reason: null },
    thresholds: { line: "DT", version: 4 },
    runs: { run: 812, walkforward: 820, optimize: 818, montecarlo: 822 },
    created_at: "2026-11-25T19:20:00Z",
    ...overrides,
  };
}

export const STRATEGIES: StrategyRow[] = [
  { strategy_id: "ema_cross_nq", version: 3, line: "DT", verdict: "no_go", verdict_id: 41, evaluated_at: "2026-11-25T19:20:00Z" },
  { strategy_id: "gap_fade_es", version: 1, line: "SWC", verdict: null, verdict_id: null, evaluated_at: null },
  { strategy_id: "lp_core", version: 2, line: "LP", verdict: null, verdict_id: null, evaluated_at: null },
];

export const SUMMARY: RunSummary = {
  run_id: 812,
  status: "done",
  realized_pnl_usd: "18420.00",
  mtm_pnl_usd: "18950.00",
  costs_usd: { commission: "2060.00", slippage: "3090.00", swap: "0", impact: "0" },
  ambiguous_bars: 3,
  stats: {
    sharpe: { value: 1.31 },
    sortino: { value: 1.88 },
    calmar: { value: 0.92 },
    drawdown: { value: { max_drawdown: -0.094, longest_dd_days: 80 } },
    drawdown_duration: { value: { max_underwater_days: 74 } },
    trade_stats: { value: { n_trades: 412, win_rate: 0.54, profit_factor: 1.41 } },
  },
  warnings: [],
};

export const WALKFORWARD: WalkForwardData = {
  scheme: "rolling",
  objective: "net_pnl",
  n_folds: 2,
  purge: 5,
  embargo: 5,
  windows: [
    { index: 0, train: [0, 1200], train_rows: 1200, test_blocks: [[1205, 1500]], chosen: { fast: 12, slow: 150 }, in_sample: 3100, out_of_sample: 1200, train_dates: ["2019-01-02", "2020-12-30"], test_dates: [["2021-01-04", "2021-06-30"]] },
    { index: 1, train: [300, 1500], train_rows: 1200, test_blocks: [[1505, 1800]], chosen: { fast: 10, slow: 140 }, in_sample: 2900.5, out_of_sample: 1100 },
  ],
  oos_total: 7300,
  efficiency: 0.62,
  leakage_free: true,
  warnings: [],
  run_id: 820,
};

export const MONTECARLO: MonteCarloData = {
  run_id: 822,
  method: "bootstrap",
  n: 1000,
  seed: 7,
  figures: {
    sharpe: { min: 0.1, percentile_5: 0.41, median: 1.22, percentile_95: 2.03, max: 2.9, mean: 1.2, std: 0.5 },
    cagr: { min: -0.1, percentile_5: 0.02, median: 0.11, percentile_95: 0.21, max: 0.3, mean: 0.11, std: 0.06 },
  },
  warnings: [],
  p_value: 0.03,
};

export const HEATMAP: HeatmapResponse = {
  svg: '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="300"><rect width="10" height="10"/></svg>',
  heatmap: {
    x: "fast",
    y: "slow",
    metric: "sharpe",
    x_values: [8, 10, 12, 14],
    y_values: [100, 160, 400],
    fixed: { stop_atr: 2 },
    cells: [],
  },
  axes: [
    { name: "fast", kind: "int" },
    { name: "slow", kind: "int" },
    { name: "stop_atr", kind: "float" },
  ],
  best: { params: { fast: 8, slow: 400 }, objective: 1.9 },
  n_trials: 300,
  failed: 4,
  cache_hits: 38,
  edge_of_space: ["slow"],
  warnings: [],
};

export const THRESHOLDS_V4: ThresholdSet = {
  id: 14,
  line: "DT",
  version: 4,
  values: {
    pbo_warn: 0.2,
    pbo_reject: 0.5,
    min_dsr: 0.95,
    min_oos_to_is: 0.5,
    max_cost_share: 0.33,
    edge_warns: true,
    neighbor_spread: 0.1,
    max_neighbor_drop: 0.3,
    red_flag_sharpe: 3,
    max_top_trade_share: 0.25,
    min_years: 1,
    min_trades: 200,
    min_regime_share: 0.5,
  },
  changed_by: "owner",
  reason: "more room for costs",
  created_at: "2026-11-24T15:00:00Z",
};

export const THRESHOLD_HISTORY: ThresholdSet[] = [
  { ...THRESHOLDS_V4, id: 11, version: 1, changed_by: "system:seed", reason: null, created_at: "2026-11-01T10:00:00Z", values: { ...THRESHOLDS_V4.values, max_cost_share: 0.25, min_dsr: 0.9 } },
  { ...THRESHOLDS_V4, id: 12, version: 2, reason: "stricter costs", created_at: "2026-11-10T10:00:00Z", values: { ...THRESHOLDS_V4.values, max_cost_share: 0.3, min_dsr: 0.9 } },
  { ...THRESHOLDS_V4, id: 13, version: 3, reason: "dsr", created_at: "2026-11-15T10:00:00Z", values: { ...THRESHOLDS_V4.values, max_cost_share: 0.3 } },
  THRESHOLDS_V4,
];

export const QUESTIONS: QuestionState[] = [
  { key: "survivorship_free", subject: "CME:NQ", status: "ask", suggestion: null, suggestion_reason: null, answer: null, answered_at: null, answer_id: null, scope: "data" },
  { key: "gaps_checked", subject: "CME:NQ", status: "ask", suggestion: "yes", suggestion_reason: "questions.reason.gaps", answer: null, answered_at: null, answer_id: null, scope: "data" },
  { key: "holdout_untouched", subject: "ema_cross_nq@3", status: "ask", suggestion: null, suggestion_reason: null, answer: null, answered_at: null, answer_id: null, scope: "strategy" },
  { key: "point_in_time", subject: "CME:NQ", status: "answered", suggestion: "yes", suggestion_reason: null, answer: "yes", answered_at: "2026-11-20T10:00:00Z", answer_id: 5, scope: "data" },
];

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function html(body: string): Response {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/html" } });
}

/** An error body like the ones the engine sends: {code, detail}. */
export function fail(status: number, code: string, detail = code): Response {
  return json({ code, detail }, status);
}
