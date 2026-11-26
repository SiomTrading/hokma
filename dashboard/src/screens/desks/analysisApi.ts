import { api } from "@/api";

/** The eight steps of an analysis, in the order the panel shows them (the same list as `desks/swo/report.py`). */
export const ANALYSIS_STEPS = ["plan", "analysts", "company_filter", "debate", "judge", "trader", "committee", "manager"] as const;
export type AnalysisNode = (typeof ANALYSIS_STEPS)[number];
export type StepState = "pending" | "running" | "done" | "stopped" | "skipped";

export interface AnalysisStep {
  node: string;
  step: number;
  state: StepState;
}

/** One agent of the run. `absent` is a report that was never going to arrive and `failed` one that was expected: both are shown as an absence with their cause. */
export interface AnalysisAgent {
  seat: string;
  name: string;
  role: string;
  step: string;
  status: "ok" | "absent" | "failed";
  summary: string;
  report: string;
  why: string;
  tools: string[];
}

export interface AnalysisProposal {
  structure: string | null;
  underlying: string | null;
  expiration: string | null;
  contracts: number | null;
  net_premium: number | string | null;
  max_loss_usd: number | string | null;
}

/** What became of the proposal at the door of execution. `needs_account`: the strategy has no account yet. */
export interface AnalysisDecision {
  proposal_id: number;
  decision_id: number | null;
  status: string;
  code: string | null;
  level: number | null;
  strategy_key: string | null;
  account_id: number | null;
  pending: boolean;
  needs_account: boolean;
}

export interface AnalysisReport {
  run_id: number;
  desk_id: number;
  status: "running" | "done" | "failed" | "interrupted";
  symbol: string | null;
  as_of: string | null;
  started_at: string | null;
  ended_at: string | null;
  steps: AnalysisStep[];
  rating: string | null;
  grade: string | null;
  summary: string | null;
  why: string[];
  proposal: AnalysisProposal | null;
  decision: AnalysisDecision | null;
  stopped_at: string | null;
  reason: string | null;
  detail: string | null;
  error: string | null;
  absent: string[];
  agents: AnalysisAgent[];
  level: number | null;
}

export interface AnalysisBrief {
  run_id: number;
  status: string;
  symbol: string | null;
  as_of: string | null;
  started_at: string | null;
  ended_at: string | null;
  rating: string | null;
  grade: string | null;
  stopped_at: string | null;
  reason: string | null;
  has_proposal: boolean;
  absent: string[];
}

export interface AnalysisList {
  desk_id: number;
  /** False on a desk that is not a stock options desk: the panel shows `message` and offers no run. */
  available: boolean;
  reason: string | null;
  message: string | null;
  analyses: AnalysisBrief[];
}

/** One message of the live feed of `/ws/chat` after `{"type":"watch_desk"}`. */
export interface AnalysisEvent {
  type: "analysis";
  event: "started" | "step" | "finished" | "failed";
  desk_id: number;
  run_id: number;
  symbol?: string;
  node?: string;
  step?: number;
  phase?: "enter" | "exit";
  ok?: boolean;
  status?: string;
  error?: string;
}

const base = (deskId: number) => `/api/desks/${deskId}/analysis`;

export const analysisApi = {
  list: (deskId: number, limit = 20) => api.get<AnalysisList>(`${base(deskId)}?limit=${limit}`),
  get: (deskId: number, runId: number) => api.get<AnalysisReport>(`${base(deskId)}/${runId}`),
  start: (deskId: number, symbol: string) => api.post<{ run_id: number; symbol: string; status: string }>(base(deskId), { symbol }),
};

/** The steps after one event of the feed: the one that enters runs, the one that leaves is done (or stopped when it did not pass). */
export function applyStep(steps: AnalysisStep[], event: AnalysisEvent): AnalysisStep[] {
  if (event.event !== "step" || !event.node) return steps;
  return steps.map((item) => {
    if (item.node !== event.node) return item;
    if (event.phase === "enter") return { ...item, state: "running" };
    return { ...item, state: event.ok === false ? "stopped" : "done" };
  });
}
