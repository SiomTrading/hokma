import type { ExpirationGroup, ExpirationItem, OptionCell, OptionRow } from "./types";

/** The filter of the chain. It works on what is loaded; it never asks for anything. Off by default. */
export interface FilterValues {
  minDays: number;
  maxDays: number;
  minDelta: number;
  maxDelta: number;
  minOpenInterest: number;
  maxSpread: number;
}

export interface FilterState {
  on: boolean;
  values: FilterValues;
}

/** The rule of the PEAD strategy: 60 to 90 days, delta 0.50 to 0.70, open interest 500, spread 5 %. */
export const PEAD_FILTER: FilterValues = { minDays: 60, maxDays: 90, minDelta: 0.5, maxDelta: 0.7, minOpenInterest: 500, maxSpread: 5 };
export const FILTER_OFF: FilterState = { on: false, values: PEAD_FILTER };

/** Spread as a percent of the mid price; null when a side of the quote is missing or zero. */
export function spreadPercent(cell: OptionCell): number | null {
  const { bid, ask } = cell;
  if (bid === null || ask === null || bid <= 0 || ask <= 0) return null;
  return ((ask - bid) / ((bid + ask) / 2)) * 100;
}

export function cellPasses(cell: OptionCell | null, values: FilterValues): boolean {
  if (!cell) return false;
  if (cell.delta === null) return false;
  const delta = Math.abs(cell.delta);
  if (delta < values.minDelta || delta > values.maxDelta) return false;
  if (cell.open_interest === null || cell.open_interest < values.minOpenInterest) return false;
  const spread = spreadPercent(cell);
  return spread !== null && spread <= values.maxSpread;
}

export function dayInRange(days: number, values: FilterValues): boolean {
  return days >= values.minDays && days <= values.maxDays;
}

/** What a row shows with the filter on: the side that does not pass is left empty. Null when neither side passes. */
export function filterRow(row: OptionRow, values: FilterValues): OptionRow | null {
  const call = cellPasses(row.call, values) ? row.call : null;
  const put = cellPasses(row.put, values) ? row.put : null;
  return call || put ? { strike: row.strike, call, put } : null;
}

export function visibleGroups(items: readonly ExpirationItem[], filter: FilterState): ExpirationItem[] {
  return filter.on ? items.filter((item) => dayInRange(item.days, filter.values)) : [...items];
}

export function visibleRows(group: ExpirationGroup, filter: FilterState): OptionRow[] {
  if (!filter.on) return group.rows;
  return group.rows.map((row) => filterRow(row, filter.values)).filter((row): row is OptionRow => row !== null);
}

/** The text of a field, read as a number; null when it is empty or not a number. */
export function readNumber(text: string): number | null {
  const normal = text.trim().replace(",", ".");
  if (normal === "") return null;
  const value = Number(normal);
  return Number.isFinite(value) ? value : null;
}
