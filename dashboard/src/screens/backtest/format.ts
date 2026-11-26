import type { PhaseId, QuantFigure, RunSummary } from "./types";

/** Display only: every number comes from what the server saved. Nothing here computes a statistic. */

export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function formatNumber(value: number | null | undefined, locale: string, digits = 2): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** 18420 gives "18.420 USD" in Spanish and "18,420 USD" in English. */
export function formatUsd(value: number | null | undefined, locale: string): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `${value.toLocaleString(locale, { maximumFractionDigits: 0 })} USD`;
}

/** A saved share (0.094) shown as a percentage ("9,4%"). Scaling for display, not a calculation of the result. */
export function formatShare(value: number | null | undefined, locale: string, digits = 1): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `${(value * 100).toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

/** A day as the owner reads it. A date with no time ("2019-01-02") is a calendar day: it is not moved by the time zone. */
export function formatDate(iso: string, locale: string): string {
  const date = parseDate(iso);
  if (!date) return iso;
  const dayOnly = /^\d{4}-\d{2}-\d{2}$/.test(iso);
  return date.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric", ...(dayOnly ? { timeZone: "UTC" } : {}) });
}

export function formatDateTime(iso: string, locale: string): string {
  const date = parseDate(iso);
  return date ? date.toLocaleString(locale, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : iso;
}

function parseDate(iso: string): Date | null {
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso.includes("T") || iso.endsWith("Z") ? iso : iso.replace(" ", "T") + "Z");
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "fast 12, slow 150": the parameters of a point, as the server names them. */
export function describeParams(params: Record<string, unknown> | null | undefined, locale: string): string {
  if (!params) return "";
  return Object.entries(params)
    .map(([name, value]) => `${name} ${typeof value === "number" ? value.toLocaleString(locale, { maximumFractionDigits: 6 }) : String(value)}`)
    .join(", ");
}

/** Reads one figure out of a block of results.summary: a plain number, or a key of the block's map. */
export function figure(stats: Record<string, QuantFigure> | undefined, block: string, key?: string): number | null {
  const entry = stats?.[block];
  if (!entry) return null;
  const value = entry.value;
  if (key === undefined) return toNumber(value);
  if (value && typeof value === "object") return toNumber((value as Record<string, unknown>)[key]);
  return null;
}

export interface FigureSet {
  net: number | null;
  mtm: number | null;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  maxDrawdown: number | null;
  drawdownDays: number | null;
  trades: number | null;
  winRate: number | null;
  profitFactor: number | null;
  costs: { commission: number | null; slippage: number | null; swap: number | null; impact: number | null };
  ambiguous: number | null;
}

/** Picks the saved figures of block C. Each one is a value the engine and hokma.quant saved; null when it was not saved. */
export function readFigures(summary: RunSummary): FigureSet {
  const stats = summary.stats;
  return {
    net: toNumber(summary.realized_pnl_usd),
    mtm: toNumber(summary.mtm_pnl_usd),
    sharpe: figure(stats, "sharpe") ?? figure(stats, "ratios", "sharpe"),
    sortino: figure(stats, "sortino") ?? figure(stats, "ratios", "sortino"),
    calmar: figure(stats, "calmar"),
    maxDrawdown: figure(stats, "drawdown", "max_drawdown"),
    drawdownDays: figure(stats, "drawdown_duration", "max_underwater_days") ?? figure(stats, "drawdown", "longest_dd_days"),
    trades: figure(stats, "trade_stats", "n_trades"),
    winRate: figure(stats, "trade_stats", "win_rate"),
    profitFactor: figure(stats, "trade_stats", "profit_factor"),
    costs: {
      commission: toNumber(summary.costs_usd?.commission),
      slippage: toNumber(summary.costs_usd?.slippage),
      swap: toNumber(summary.costs_usd?.swap),
      impact: toNumber(summary.costs_usd?.impact),
    },
    ambiguous: toNumber(summary.ambiguous_bars),
  };
}

/** Code of a notice: what is before the first ":"; the rest is its parameter. */
export function splitCode(raw: string): { code: string; arg: string } {
  const at = raw.indexOf(":");
  return at < 0 ? { code: raw.trim(), arg: "" } : { code: raw.slice(0, at).trim(), arg: raw.slice(at + 1).trim() };
}

export const NOTICE_CODES = [
  "many_trials",
  "trials_below_columns",
  "costs_kill_edge",
  "cost_in_sharpe_unavailable",
  "ambiguous_bars",
  "monte_carlo_missing",
  "analysis_part_missing",
  "historical_margin_missing",
  "below_realistic_expectation",
  "data_changed",
  "edge_of_space",
  "presweep_not_available",
  "presweep_top_k",
  "constraint_rejections",
  "failed_trials",
  "no_valid_trial",
  "size_zero",
  "entry_ignored_no_accumulation",
  "optimization_unreadable",
  "evidence_unavailable",
  "closed_by_expiry",
  "closed_by_end_of_data",
  "closed_by_account_blown",
] as const;

export function phaseKey(phase: PhaseId): string {
  return `verdict.phase.${phase}`;
}

/** The SVG as an image source: base64 inside a data URL, so the browser never parses it as part of the page. */
export function svgDataUrl(svg: string): string {
  const bytes = new TextEncoder().encode(svg);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}
