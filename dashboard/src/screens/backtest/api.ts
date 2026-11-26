import { useSyncExternalStore } from "react";
import { api, ApiError, request } from "@/api";
import type {
  AnswerInput,
  EvaluateResponse,
  HeatmapResponse,
  JobStatus,
  LineId,
  MonteCarloData,
  QuestionState,
  StrategyRow,
  ThresholdSet,
  Verdict,
  WalkForwardData,
} from "./types";
import type { RunSummary } from "./types";

const enc = encodeURIComponent;

/* ---------- The owner confirmation (X-Hokma-Owner) ---------- */

const OWNER_CODES = new Set(["owner_confirmation_required", "bad_owner_nonce"]);
let ownerBlocked = false;
const listeners = new Set<() => void>();

function emit(): void {
  listeners.forEach((listener) => listener());
}

export function setOwnerBlocked(value: boolean): void {
  if (ownerBlocked === value) return;
  ownerBlocked = value;
  emit();
}

/**
 * True once the engine refused a call of the owner for lack of the confirmation (403 owner_confirmation_required, after the
 * client already tried to register it again). The screen then shows everything and disables what writes. A call of the owner
 * that goes through clears it.
 */
export function useOwnerBlocked(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => ownerBlocked,
    () => ownerBlocked,
  );
}

/** Runs a call that needs the owner and keeps track of whether the confirmation was accepted. */
export async function asOwner<T>(call: () => Promise<T>): Promise<T> {
  try {
    const out = await call();
    setOwnerBlocked(false);
    return out;
  } catch (cause) {
    if (cause instanceof ApiError && OWNER_CODES.has(cause.code)) setOwnerBlocked(true);
    throw cause;
  }
}

/** Test helper. */
export function resetOwnerGate(): void {
  setOwnerBlocked(false);
}

/* ---------- Errors ---------- */

/** Every code the error carries (code, reason, and the detail of the body): the server may use any of them. */
export function errorCodes(cause: unknown): string[] {
  if (!(cause instanceof ApiError)) return [];
  const out = [cause.code, cause.reason];
  for (const field of ["code", "reason"]) {
    const value = cause.data?.[field];
    if (typeof value === "string") out.push(value);
  }
  return out.filter((value) => value.length > 0);
}

export function hasCode(cause: unknown, code: string): boolean {
  return errorCodes(cause).includes(code);
}

/** The text of a classified error of a job ({code, message}): the translated sentence of the code, else the generic one. */
export function jobErrorText(error: { code: string }, translate: (key: string) => string): string {
  const key = `verdict.error.code.${error.code}`;
  const text = translate(key);
  return text === key ? translate("verdict.error.generic") : text;
}

export function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError && cause.message ? cause.message : fallback;
}

/* ---------- Routes ---------- */

export const verdictApi = {
  strategies: () => api.get<StrategyRow[]>("/api/verdict/strategies"),
  latest: (strategy: string, version: number) => api.get<Verdict>(`/api/verdict/${enc(strategy)}/${version}`),
  evaluate: (strategy: string, version: number) => api.post<EvaluateResponse>(`/api/verdict/${enc(strategy)}/${version}/evaluate`),
  job: (jobId: number) => api.get<JobStatus>(`/api/verdict/jobs/${jobId}`),
  exclude: (verdictId: number, keys: string[], reason?: string) =>
    asOwner(() => api.post<Verdict>(`/api/verdict/${verdictId}/exclude`, reason ? { keys, reason } : { keys })),
  include: (verdictId: number, keys: string[]) => asOwner(() => api.post<Verdict>(`/api/verdict/${verdictId}/include`, { keys })),
  recompute: (verdictId: number, cause: "thresholds" | "answers") =>
    // A new neighbor spread can run backtests: no timeout, the screen shows "recomputing" until the server answers.
    asOwner(() => api.post<Verdict>(`/api/verdict/${verdictId}/recompute`, { cause }, { timeoutMs: 0 })),

  questions: (strategy: string, version: number) => api.get<QuestionState[]>(`/api/verdict/${enc(strategy)}/${version}/questions`),
  answers: (strategy: string, version: number, answers: AnswerInput[]) =>
    asOwner(() => api.post<unknown>(`/api/verdict/${enc(strategy)}/${version}/answers`, { answers })),

  thresholds: (line: LineId) => api.get<ThresholdSet>(`/api/verdict/thresholds/${line}`),
  history: (line: LineId) => api.get<ThresholdSet[]>(`/api/verdict/thresholds/${line}/history`),
  saveThresholds: (line: LineId, values: Record<string, number | boolean>, reason?: string) =>
    asOwner(() => api.put<ThresholdSet>(`/api/verdict/thresholds/${line}`, reason ? { values, reason } : { values })),
  restoreThresholds: (line: LineId, setId: number) =>
    asOwner(() => api.post<ThresholdSet>(`/api/verdict/thresholds/${line}/restore/${setId}`)),
  copyThresholds: (line: LineId, source: LineId) =>
    asOwner(() => api.post<ThresholdSet>(`/api/verdict/thresholds/${line}/copy-from/${source}`)),

  report: async (runId: number): Promise<string> => (await request(`/api/verdict/runs/${runId}/report`)).text(),
  runMonteCarlo: (runId: number) => asOwner(() => api.post<MonteCarloData>(`/api/verdict/runs/${runId}/montecarlo`)),
};

export const paths = {
  strategies: "/api/verdict/strategies",
  latest: (strategy: string, version: number) => `/api/verdict/${enc(strategy)}/${version}`,
  questions: (strategy: string, version: number) => `/api/verdict/${enc(strategy)}/${version}/questions`,
  thresholds: (line: LineId) => `/api/verdict/thresholds/${line}`,
  history: (line: LineId) => `/api/verdict/thresholds/${line}/history`,
  summary: (runId: number) => `/api/verdict/runs/${runId}/summary`,
  walkforward: (runId: number) => `/api/verdict/runs/${runId}/walkforward`,
  montecarlo: (runId: number) => `/api/verdict/runs/${runId}/montecarlo`,
  heatmap: (optId: number, x?: string, y?: string, metric?: string) => {
    const query = new URLSearchParams();
    if (x) query.set("x", x);
    if (y) query.set("y", y);
    if (metric) query.set("metric", metric);
    const text = query.toString();
    return `/api/verdict/optimizations/${optId}/heatmap${text ? `?${text}` : ""}`;
  },
};

export type { HeatmapResponse, MonteCarloData, RunSummary, WalkForwardData };
