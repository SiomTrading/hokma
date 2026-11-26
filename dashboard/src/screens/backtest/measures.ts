import type { MeasureValue } from "./types";

export type MeasureKind = "share" | "number" | "years" | "count" | "toggle";

export interface MeasureMeta {
  /** Id of section 6 of SPEC-032: M1 to M13. The texts of the explanations are verdict.measure.{id}.*. */
  id: string;
  /** Key of the threshold table (thresholds.MEASURES). */
  key: string;
  kind: MeasureKind;
  low: number;
  high: number;
  step: number;
}

/** The 13 measures of section 6, in order: ranges and steps of the editor (the server validates them again). */
export const MEASURES: readonly MeasureMeta[] = [
  { id: "M1", key: "pbo_warn", kind: "share", low: 0.05, high: 0.95, step: 0.05 },
  { id: "M2", key: "pbo_reject", kind: "share", low: 0.05, high: 0.95, step: 0.05 },
  { id: "M3", key: "min_dsr", kind: "share", low: 0.5, high: 0.99, step: 0.01 },
  { id: "M4", key: "min_oos_to_is", kind: "number", low: 0.1, high: 1, step: 0.05 },
  { id: "M5", key: "max_cost_share", kind: "share", low: 0.05, high: 1, step: 0.01 },
  { id: "M6", key: "edge_warns", kind: "toggle", low: 0, high: 1, step: 1 },
  { id: "M7", key: "neighbor_spread", kind: "share", low: 0.05, high: 0.5, step: 0.05 },
  { id: "M8", key: "max_neighbor_drop", kind: "share", low: 0.05, high: 1, step: 0.05 },
  { id: "M9", key: "red_flag_sharpe", kind: "number", low: 1, high: 10, step: 0.1 },
  { id: "M10", key: "max_top_trade_share", kind: "share", low: 0.05, high: 1, step: 0.05 },
  { id: "M11", key: "min_years", kind: "years", low: 0.5, high: 30, step: 0.5 },
  { id: "M12", key: "min_trades", kind: "count", low: 10, high: 10000, step: 10 },
  { id: "M13", key: "min_regime_share", kind: "share", low: 0.1, high: 1, step: 0.05 },
];

/** Decimals of a step (0.05 gives 2): keeps 0.1 + 0.2 style noise out of the values the owner sees and saves. */
export function stepDecimals(step: number): number {
  const text = String(step);
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : text.length - dot - 1;
}

export function clampMeasure(meta: MeasureMeta, value: number): number {
  const bounded = Math.min(meta.high, Math.max(meta.low, value));
  return Number(bounded.toFixed(stepDecimals(meta.step)));
}

export function inRange(meta: MeasureMeta, value: number): boolean {
  return Number.isFinite(value) && value >= meta.low && value <= meta.high;
}

export function numericValue(value: MeasureValue | undefined, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}
