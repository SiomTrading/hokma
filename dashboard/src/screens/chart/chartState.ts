/**
 * What each chart tab remembers in this browser (SPEC-001 addendum, chart state): its interval and its view (zoom,
 * scroll, follow-the-right-edge, price scale), under `hokma.chart.tabs.v1`, by tab key. The asset is not here: it is
 * the `symbol` param of the tab. `last` is the chart the student looked at last; a new chart tab opens with it.
 *
 * Nothing here is sensitive and nothing here goes to the engine. Storage that is missing, full or holds something
 * unreadable never breaks a chart: reads give the defaults, writes are skipped.
 */
import { loadInterval } from "./favorites";
import { canonicalInterval } from "./intervals";

export const TABS_KEY = "hokma.chart.tabs.v1";
/** Entries kept in `tabs`; past this the ones saved longest ago go. `last` never goes. */
export const MAX_TABS = 50;
/** The view is written this long after the last movement. */
export const VIEW_DELAY_MS = 300;

export type PriceScaleState = { auto: true } | { auto: false; min: number; max: number };

export interface ChartViewState {
  /** The right edge is within half a bar of the last bar: new bars keep entering by the right. */
  follow: boolean;
  barSpacing: number;
  /** Bars between the last bar and the right edge (only meaningful when `follow`). */
  rightOffset: number;
  /** Real time (epoch s) of the first and the last bar in view; range bars are stored by real time too. */
  from: number;
  to: number;
  price: PriceScaleState;
}

export interface TabEntry {
  tf: string;
  /** Null is the default view: fit all bars. */
  view: ChartViewState | null;
  /** Reserved for the indicators session: always empty in this one. Kept as found when it is a list. */
  indicators: unknown[];
  updated_at: number;
}

export interface LastEntry {
  symbol: string;
  tf: string;
  view: ChartViewState | null;
  updated_at: number;
}

interface Store {
  v: 1;
  tabs: Record<string, TabEntry>;
  last: LastEntry | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const SYMBOL = /^[A-Za-z0-9^=.\-:/]{1,40}$/;

function parsePrice(value: unknown): PriceScaleState | null {
  if (!isRecord(value) || typeof value.auto !== "boolean") return null;
  if (value.auto) return { auto: true };
  if (isNumber(value.min) && isNumber(value.max) && value.min < value.max) return { auto: false, min: value.min, max: value.max };
  return null;
}

/** A view that is not exactly what we wrote (or cannot be drawn) is no view: the chart opens fitted. */
export function parseView(value: unknown): ChartViewState | null {
  if (!isRecord(value)) return null;
  const { follow, barSpacing, rightOffset, from, to } = value;
  const price = parsePrice(value.price);
  if (typeof follow !== "boolean" || !isNumber(barSpacing) || !isNumber(rightOffset) || !isNumber(from) || !isNumber(to) || !price) return null;
  if (barSpacing <= 0 || barSpacing > 500 || from > to) return null;
  return { follow, barSpacing, rightOffset, from, to, price };
}

function parseTab(value: unknown): TabEntry | null {
  if (!isRecord(value)) return null;
  const updated = isNumber(value.updated_at) ? value.updated_at : 0;
  const indicators = Array.isArray(value.indicators) ? value.indicators : [];
  const tf = typeof value.tf === "string" ? canonicalInterval(value.tf) : null;
  // A rejected interval becomes the default one, and a view only means something with the interval it was saved for.
  if (!tf) return { tf: loadInterval(), view: null, indicators, updated_at: updated };
  return { tf, view: parseView(value.view), indicators, updated_at: updated };
}

function parseLast(value: unknown): LastEntry | null {
  if (!isRecord(value) || typeof value.symbol !== "string" || !SYMBOL.test(value.symbol)) return null;
  const tf = typeof value.tf === "string" ? canonicalInterval(value.tf) : null;
  if (!tf) return null;
  return { symbol: value.symbol, tf, view: parseView(value.view), updated_at: isNumber(value.updated_at) ? value.updated_at : 0 };
}

function load(): Store {
  const empty: Store = { v: 1, tabs: {}, last: null };
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(TABS_KEY);
  } catch {
    return empty;
  }
  if (raw === null) return empty;
  try {
    const data: unknown = JSON.parse(raw);
    if (!isRecord(data) || data.v !== 1) return empty;
    const tabs: Record<string, TabEntry> = {};
    if (isRecord(data.tabs)) {
      for (const [key, value] of Object.entries(data.tabs)) {
        const entry = parseTab(value);
        if (entry) tabs[key] = entry;
      }
    }
    return { v: 1, tabs, last: parseLast(data.last) };
  } catch {
    return empty;
  }
}

function save(store: Store, keep?: string): void {
  const keys = Object.keys(store.tabs);
  if (keys.length > MAX_TABS) {
    const oldest = keys.filter((key) => key !== keep).sort((a, b) => store.tabs[a]!.updated_at - store.tabs[b]!.updated_at);
    for (const key of oldest.slice(0, keys.length - MAX_TABS)) delete store.tabs[key];
  }
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(store));
  } catch {
    /* storage unavailable or full */
  }
}

const now = () => Math.floor(Date.now() / 1000);

/** Reads, changes and writes in one step. */
function mutate(change: (store: Store) => string | undefined): void {
  const store = load();
  const keep = change(store);
  save(store, keep);
}

// ---- reads ---------------------------------------------------------------------------------------------------------

export function readTab(key: string): TabEntry | null {
  return load().tabs[key] ?? null;
}

export function readLast(): LastEntry | null {
  return load().last;
}

/** The interval a chart tab with nothing saved opens with: the last chart's, else the old global one, else `5m`. */
export function defaultInterval(): string {
  return readLast()?.tf ?? loadInterval();
}

export function tabKeys(): string[] {
  return Object.keys(load().tabs);
}

// ---- writes --------------------------------------------------------------------------------------------------------

function setLast(store: Store, symbol: string, entry: TabEntry): void {
  store.last = { symbol, tf: entry.tf, view: entry.view, updated_at: now() };
}

/**
 * The tab is now in front (or was just mounted in front): its entry is created if it had none, and it becomes the last
 * chart seen.
 */
export function activateTab(key: string, symbol: string, tf: string, view: ChartViewState | null): void {
  mutate((store) => {
    const entry = store.tabs[key] ?? { tf, view, indicators: [], updated_at: now() };
    entry.updated_at = now();
    store.tabs[key] = entry;
    setLast(store, symbol, entry);
    return key;
  });
}

/** A new interval: the view goes back to the default one. `front` says if the tab is the one being looked at. */
export function saveTabInterval(key: string, symbol: string, tf: string, front: boolean): void {
  const canonical = canonicalInterval(tf);
  if (!canonical) return;
  mutate((store) => {
    const entry: TabEntry = { tf: canonical, view: null, indicators: store.tabs[key]?.indicators ?? [], updated_at: now() };
    store.tabs[key] = entry;
    if (front) setLast(store, symbol, entry);
    return key;
  });
}

function writeView(key: string, symbol: string, tf: string, view: ChartViewState, front: boolean): void {
  const canonical = canonicalInterval(tf);
  if (!canonical) return;
  mutate((store) => {
    const entry: TabEntry = { tf: canonical, view, indicators: store.tabs[key]?.indicators ?? [], updated_at: now() };
    store.tabs[key] = entry;
    if (front) setLast(store, symbol, entry);
    return key;
  });
}

interface Pending {
  symbol: string;
  tf: string;
  view: ChartViewState;
  front: boolean;
  timer: ReturnType<typeof setTimeout>;
}
const pending = new Map<string, Pending>();

/** Writes the view `VIEW_DELAY_MS` after the last call for this tab; a newer call replaces the waiting one. */
export function queueView(key: string, symbol: string, tf: string, view: ChartViewState, front: boolean): void {
  const waiting = pending.get(key);
  if (waiting) clearTimeout(waiting.timer);
  const timer = setTimeout(() => {
    pending.delete(key);
    writeView(key, symbol, tf, view, front);
  }, VIEW_DELAY_MS);
  pending.set(key, { symbol, tf, view, front, timer });
}

/** Writes every waiting view now: the page is going away, or the screen is closing. */
export function flushPending(): void {
  for (const [key, item] of [...pending]) {
    clearTimeout(item.timer);
    pending.delete(key);
    writeView(key, item.symbol, item.tf, item.view, item.front);
  }
}

/** Forgets the view waiting for this tab (its interval or its asset just changed, so that view is already stale). */
export function cancelPending(key: string): void {
  const waiting = pending.get(key);
  if (!waiting) return;
  clearTimeout(waiting.timer);
  pending.delete(key);
}

/**
 * The tab changes key because its asset changed. The entry moves to the new key keeping the interval and going back to
 * the default view. When `merged` the new key belongs to another tab that stays as it is and this entry just goes.
 */
export function moveTab(from: string, to: string, merged: boolean): void {
  if (from === to) return;
  cancelPending(from);
  mutate((store) => {
    const entry = store.tabs[from];
    delete store.tabs[from];
    if (merged || !entry) return undefined;
    store.tabs[to] = { tf: entry.tf, view: null, indicators: entry.indicators, updated_at: now() };
    return to;
  });
}

/** Opens a key with a given state (a new chart tab that opens with the last chart). */
export function seedTab(key: string, tf: string, view: ChartViewState | null): void {
  mutate((store) => {
    store.tabs[key] = { tf, view, indicators: [], updated_at: now() };
    return key;
  });
}

export function dropTab(key: string): void {
  cancelPending(key);
  mutate((store) => {
    delete store.tabs[key];
    return undefined;
  });
}

/** Cleanup on load: only the tabs that are open keep an entry. `last` stays. */
export function dropClosed(openKeys: readonly string[]): void {
  const open = new Set(openKeys);
  const store = load();
  const stale = Object.keys(store.tabs).filter((key) => !open.has(key));
  if (stale.length === 0) return;
  for (const key of stale) delete store.tabs[key];
  save(store);
}

// A reload or a closed window right after a movement must not lose it.
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flushPending);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushPending();
  });
}
