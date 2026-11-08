import { canonicalInterval, compareIntervals } from "./intervals";

/** What the chart remembers in this browser. Storage that is missing or full never breaks the chart. */
export const INTERVALS_KEY = "hokma.chart.intervals";
export const RECENT_KEY = "hokma.chart.recent";
export const TIMEFRAME_KEY = "hokma.chart.timeframe";

export const MAX_FAVORITES = 8;
export const MAX_RECENT = 8;
export const DEFAULT_FAVORITES: readonly string[] = ["1m", "5m", "15m", "1h", "1D"];
export const DEFAULT_INTERVAL = "5m";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable or full */
  }
}

function readList(key: string): unknown[] | null {
  const raw = read(key);
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

/** Valid, canonical, without repeats, at most 8, in display order. */
export function normalizeFavorites(list: readonly unknown[]): string[] {
  const out: string[] = [];
  for (const item of list) {
    if (typeof item !== "string") continue;
    const canonical = canonicalInterval(item);
    if (canonical && !out.includes(canonical)) out.push(canonical);
  }
  return out.sort(compareIntervals).slice(0, MAX_FAVORITES);
}

/** The favorite intervals. Nothing saved, or something unreadable, gives the five defaults. */
export function loadFavorites(): string[] {
  const list = readList(INTERVALS_KEY);
  return list === null ? [...DEFAULT_FAVORITES] : normalizeFavorites(list);
}

export function saveFavorites(list: readonly string[]): string[] {
  const clean = normalizeFavorites(list);
  write(INTERVALS_KEY, JSON.stringify(clean));
  return clean;
}

/**
 * Pins or unpins one interval and saves the result. When there are already 8 the list does not change: the caller
 * shows `chart.interval.favoritesFull`.
 */
export function toggleFavorite(list: readonly string[], interval: string): string[] {
  const canonical = canonicalInterval(interval);
  const current = normalizeFavorites(list);
  if (!canonical) return current;
  if (current.includes(canonical)) return saveFavorites(current.filter((item) => item !== canonical));
  if (current.length >= MAX_FAVORITES) return current;
  return saveFavorites([...current, canonical]);
}

export function isFavorite(list: readonly string[], interval: string): boolean {
  const canonical = canonicalInterval(interval);
  return !!canonical && list.includes(canonical);
}

const SYMBOL = /^[A-Za-z0-9^=.\-:/]{1,40}$/;

/** The last symbols picked, newest first. */
export function loadRecent(): string[] {
  const list = readList(RECENT_KEY) ?? [];
  const out: string[] = [];
  for (const item of list) {
    if (typeof item !== "string" || !SYMBOL.test(item)) continue;
    if (!out.some((seen) => seen.toUpperCase() === item.toUpperCase())) out.push(item);
  }
  return out.slice(0, MAX_RECENT);
}

/** Puts a symbol first in the recent list (moving it if it was there already) and saves it. */
export function pushRecent(symbol: string): string[] {
  const value = symbol.trim();
  if (!SYMBOL.test(value)) return loadRecent();
  const next = [value, ...loadRecent().filter((item) => item.toUpperCase() !== value.toUpperCase())].slice(0, MAX_RECENT);
  write(RECENT_KEY, JSON.stringify(next));
  return next;
}

/** The interval the chart opens with: any valid value of the grammar, old ones (`1m` ... `1D`) included. */
export function loadInterval(): string {
  const stored = read(TIMEFRAME_KEY);
  return (stored !== null && canonicalInterval(stored)) || DEFAULT_INTERVAL;
}

export function saveInterval(interval: string): void {
  const canonical = canonicalInterval(interval);
  if (canonical) write(TIMEFRAME_KEY, canonical);
}
