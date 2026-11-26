/**
 * Shapes of /api/verdict/* (SPEC-032, sections 9.2 and 12). This file is the contract the backend route module
 * (backend/hokma/verdict/api.py) has to respect; the screen reads nothing else.
 *
 * What the spec gives exactly:
 * - GET  /api/verdict/{strategy_id}/{version}            the verdict of 9.2, in the language of the owner (Verdict).
 * - POST /api/verdict/{strategy_id}/{version}/evaluate   queues verdict.evaluate (EvaluateResponse).
 * - GET  /api/verdict/jobs/{job_id}                      the state of that evaluation: progress and, when it failed, the classified error (JobStatus).
 * - POST /api/verdict/{verdict_id}/exclude | include     body {keys, reason?} (owner).
 * - POST /api/verdict/{verdict_id}/recompute             body {cause: "thresholds" | "answers"} (owner).
 * - GET  /api/verdict/{strategy_id}/{version}/questions  questions.states (QuestionState[]).
 * - POST /api/verdict/{strategy_id}/{version}/answers    body {answers: [{key, value, note?}]} (owner).
 * - GET  /api/verdict/thresholds/{line} and /history     ThresholdSet and ThresholdSet[].
 * - PUT  /api/verdict/thresholds/{line}                  body {values, reason?} (owner).
 * - POST /api/verdict/thresholds/{line}/restore/{set_id} and /copy-from/{source} (owner): a new ThresholdSet.
 * - GET  /api/verdict/runs/{run_id}/report               the HTML of results.report_html (text/html).
 * - GET  /api/verdict/optimizations/{opt_id}/heatmap     see HeatmapResponse.
 * - GET  /api/verdict/runs/{run_id}/walkforward and /montecarlo   what is saved in backtest_runs.summary_json.
 * - POST /api/verdict/runs/{run_id}/montecarlo           runs Monte Carlo with the H4 defaults (owner).
 * - GET  /api/verdict/strategies                         StrategyRow[].
 *
 * What the screen assumes where the spec is silent (the backend agent must respect it, or tell the screen):
 * 1. exclude, include and recompute answer with the NEW verdict (the 9.2 JSON). The screen swaps it in without another GET.
 * 2. Ids in the paths of runs are the ids the verdict already carries: `runs.run` for /report, /summary and the POST of
 *    /montecarlo; `runs.walkforward` for /walkforward; `runs.montecarlo` for GET /montecarlo; `runs.optimize` for the heatmap.
 *    The POST of /montecarlo answers the MonteCarloResult.to_dict() of the new run.
 * 3. GET of the verdict that never existed: 404 with code `no_verdict`. A strategy version with no finished run: 404 with
 *    code `no_run` (also on evaluate). A line with no thresholds: 400 `bad_input` with reason `no_thresholds`.
 * 4. NOT IN SECTION 12, needed by block C (figures): GET /api/verdict/runs/{run_id}/summary returns the summary_json
 *    saved by results.record_result (RunSummary: costs_usd, ambiguous_bars, stats, warnings). Without it block C says
 *    there are no saved figures; nothing is computed in the browser.
 * 5. Optional fields of the verdict that make the screen richer (the screen works without them):
 *    `notices` (stable codes of the bridge, as "code" or "code:parameter"), `evaluating` (progress while neighbors run)
 *    and `ideal.plateau_mean`.
 * 6. Optional fields of a walk-forward window: `train_dates` and `test_dates` (ISO dates). WalkForwardResult.to_dict() only
 *    has row numbers; with dates the table shows them, without dates it shows "rows a to b".
 * 7. GET /thresholds/{line}/history returns every version (the screen sorts them and compares each one with the one before).
 * 8. Question states: `suggestion_reason` is shown only as a tooltip, so a message key is fine; `scope` is optional (the screen
 *    infers "data" when the subject is not "{strategy_id}@{version}").
 */

export type VerdictState = "go" | "review" | "needs_evidence" | "no_go";
export type CheckStatus = "pass" | "warn" | "fail" | "unknown" | "na";
export type PhaseId = "data_integrity" | "specification" | "execution" | "statistics" | "overfitting" | "reporting";
export type LineId = "DT" | "SWC" | "SWO" | "LP" | "RF";
export type AnswerValue = "yes" | "no" | "na";

export const PHASES: readonly PhaseId[] = ["data_integrity", "specification", "execution", "statistics", "overfitting", "reporting"];
export const VERDICT_STATES: readonly VerdictState[] = ["go", "review", "needs_evidence", "no_go"];

export interface VerdictCheck {
  number: number;
  key: string;
  phase: PhaseId;
  status: CheckStatus;
  note: string;
  excluded: boolean;
}

export interface VerdictExclusion {
  key: string;
  reason: string | null;
  by: string;
  at: string;
}

export interface OpenQuestion {
  key: string;
  scope: "strategy" | "data";
  subject: string;
}

export interface Suggestion {
  check: number;
  key: string;
  status: CheckStatus;
  action: string;
  text: string;
  params: Record<string, unknown>;
}

export interface Ideal {
  status: "found" | "not_found";
  params: Record<string, number | string> | null;
  plateau_size: number | null;
  plateau_mean?: number | null;
  run_id: number | null;
  reason: string | null;
}

export interface VerdictRuns {
  run: number;
  walkforward: number | null;
  optimize: number | null;
  montecarlo: number | null;
}

export interface Verdict {
  verdict_id: number;
  strategy_id: string;
  strategy_version: number;
  line: LineId;
  verdict: VerdictState;
  audit_verdict: VerdictState;
  advisory: true;
  sentences: string[];
  checks: VerdictCheck[];
  excluded: VerdictExclusion[];
  open_questions: OpenQuestion[];
  suggestions: Suggestion[];
  ideal: Ideal;
  thresholds: { line: LineId; version: number };
  runs: VerdictRuns;
  created_at: string;
  /** The notes of the audit that made this verdict (codes then text), as the module keeps them. The screen reads `notices` instead. */
  notes?: string[];
  notices?: string[];
  evaluating?: { done: number; total: number } | null;
}

export interface StrategyRow {
  strategy_id: string;
  version: number;
  line: LineId;
  verdict: VerdictState | null;
  verdict_id: number | null;
  evaluated_at: string | null;
}

export interface EvaluateResponse {
  job_id: number;
  state: JobState;
}

export type JobState = "queued" | "running" | "done" | "failed";

/** GET /api/verdict/jobs/{job_id}: where an evaluation is. `done` and `total` are the neighbors of the step being run (total is 0 at the start). */
export interface JobStatus {
  job_id: number;
  kind: "verdict";
  strategy_id: string;
  version: number;
  state: JobState;
  done: number;
  total: number;
  verdict_id: number | null;
  error: { code: string; message: string } | null;
}

/** One block of results.summary: QuantResult.to_dict() (the value is a number or a map of numbers). */
export interface QuantFigure {
  name?: string;
  value: number | Record<string, unknown> | null;
  unit?: string;
  warnings?: string[];
}

/** The summary_json of a finished run (backtest/results.py: to_dict without trades and equity). Decimals arrive as text. */
export interface RunSummary {
  run_id: number;
  status: string;
  realized_pnl_usd: string;
  mtm_pnl_usd: string;
  costs_usd: Record<string, string>;
  ambiguous_bars: number;
  stats: Record<string, QuantFigure>;
  warnings: string[];
  /** What H4 saves next to the figures; the screen does not show them. */
  ambiguous_bar_dates?: string[];
  data_hash?: string;
  spec_hash?: string;
  engine_version?: string;
  seed?: number;
  rejected?: unknown;
}

export interface WalkForwardWindow {
  index: number;
  train: [number, number];
  train_rows: number;
  test_blocks: [number, number][];
  chosen: Record<string, number | string>;
  in_sample: number;
  out_of_sample: number;
  train_dates?: [string, string];
  test_dates?: [string, string][];
}

/** WalkForwardResult.to_dict() (backtest/walkforward.py). */
export interface WalkForwardData {
  scheme: "rolling" | "anchored" | "cpcv";
  objective: string;
  n_folds: number;
  purge: number;
  embargo: number;
  windows: WalkForwardWindow[];
  oos_total: number | null;
  efficiency: number | null;
  leakage_free: boolean;
  warnings: string[];
  run_id: number | null;
}

export interface Spread {
  min: number;
  percentile_5: number;
  median: number;
  percentile_95: number;
  max: number;
  mean: number;
  std: number;
  /** Next to the percentiles that the engine of H4 saves; the screen does not show them. */
  percentile_50?: number;
  runs?: number;
  seed?: number;
}

/** MonteCarloResult.to_dict() (backtest/placebo.py). figures: name to Spread, plus plain values for the placebo. */
export interface MonteCarloData {
  run_id: number;
  method: "bootstrap" | "permutation" | "placebo";
  n: number;
  seed: number;
  figures: Record<string, Spread | number | string | null>;
  warnings: string[];
  p_value: number | null;
}

export interface HeatmapCell {
  x: number | string;
  y: number | string;
  value: number | null;
  status: string;
}

/** Heatmap.to_dict() (backtest/surface.py). */
export interface HeatmapData {
  x: string;
  y: string;
  metric: string;
  x_values: (number | string)[];
  y_values: (number | string)[];
  fixed: Record<string, number | string>;
  cells: HeatmapCell[];
}

export interface OptimizationAxis {
  name: string;
  kind: "int" | "float" | "choice";
}

/**
 * Answer of GET /optimizations/{opt_id}/heatmap?x=&y=&metric=. Without x and y the server picks the two axes (the first two
 * when there are more than two). `svg` is Heatmap.svg() as text; the screen shows it as an image, never as HTML. `layout`
 * is the geometry of that SVG (surface.Heatmap.svg: cell, left, top, gap and the 38 px under the cells); the screen uses
 * its defaults when the field is absent. The rest comes from the OptimizationResult saved with the run.
 */
export interface HeatmapResponse {
  svg: string;
  heatmap: HeatmapData;
  axes: OptimizationAxis[];
  best: { params: Record<string, number | string>; objective: number | null } | null;
  n_trials: number;
  failed: number;
  cache_hits: number;
  edge_of_space: string[];
  warnings: string[];
  layout?: { cell: number; left: number; top: number; gap: number; bottom: number };
}

export interface QuestionState {
  key: string;
  subject: string;
  status: "ask" | "answered" | "na" | "auto";
  suggestion: "yes" | "no" | null;
  suggestion_reason: string | null;
  answer: AnswerValue | null;
  answered_at: string | null;
  answer_id: number | null;
  note?: string | null;
  /** When status is "na": a message key, verdict.na.{reason}. */
  na_reason?: string | null;
  scope?: "strategy" | "data";
}

export type MeasureValue = number | boolean;

export interface ThresholdSet {
  id: number;
  line: LineId;
  version: number;
  values: Record<string, MeasureValue>;
  changed_by: string;
  reason: string | null;
  created_at: string;
  restored_from?: number | null;
  /** True when a save, restore or copy wrote nothing because the set was already the current one: the current set comes back. */
  unchanged?: boolean;
}

export interface AnswerInput {
  key: string;
  value: AnswerValue;
  note?: string;
}
