import { t as translate } from "@/i18n";
import type { Params } from "@/i18n";
import { codeText } from "@/screens/connections/api";
import type { AssetKind, Candle, ChartData, OpeningRange, ProtocolInterval, Timeframe } from "./types";

export type DataState = "live" | "delayed";
/** What the engine says about one subscription. */
export type SubStatus = "live" | "delayed" | "reconnecting" | "closed";
/**
 * Why a range cannot be written in ticks for this symbol, for the interval box (`ticksDisabledReason`): the instrument
 * has no known tick size. Null until the first snapshot says what the tick is.
 */
export function ticksDisabledReason(state: LiveState, t: Translate = translate): string | null {
  return state.ready && state.tickSize === null ? t("chart.interval.ticksUnavailable") : null;
}

/** The size a price range was rounded to (the nearest tick), or null when it is what the student wrote. */
export function roundedRange(state: Pick<LiveState, "interval">): number | null {
  const interval = state.interval;
  return interval && interval.kind === "range" && interval.rounded === true ? interval.size : null;
}

/** What the status bar shows. */
export type BarMode = "live" | "delayed" | "reconnecting" | "closed";

export interface LiveError {
  code: string;
  message: string;
  /** Values for the catalog text (`market.interval_unavailable` carries interval, base, source and symbol). */
  params: Record<string, string | number>;
}

/**
 * A warning pill of the header, worked out from the snapshot:
 * - `seconds_start`: seconds bars, built from the trades Hokma has kept since `time`;
 * - `approx_before`: range bars before `time` come from 1-minute bars;
 * - `approx_all`: range bars all come from 1-minute bars (no trades): the header also offers to connect a source.
 */
export type ChartNotice = { kind: "seconds_start"; time: number } | { kind: "approx_before"; time: number } | { kind: "approx_all" };

export interface LiveState {
  symbol: string;
  timeframe: Timeframe;
  /** True from the first snapshot on. */
  ready: boolean;
  /** The WebSocket to the engine is open. */
  link: boolean;
  bars: Candle[];
  name: string;
  kind: AssetKind;
  exchange: string;
  currency: string;
  timezone: string;
  assetClass: string | null;
  /** True when no provider serves the symbol and the delayed fallback does. */
  fallback: boolean;
  source: string;
  dataState: DataState;
  status: SubStatus;
  delaySeconds: number | null;
  /** Where the bars before the first live trade come from (always the polling source) and how late they are. */
  history: { source: string; delaySeconds: number } | null;
  price: number | null;
  previousClose: number | null;
  change: number | null;
  changePct: number | null;
  openingRange: OpeningRange | null;
  marketOpen: boolean;
  lastTsMs: number | null;
  /** What the engine builds; null until the snapshot says it. */
  interval: ProtocolInterval | null;
  /** Epoch seconds of the first trade Hokma kept for the symbol, or null. */
  exactFrom: number | null;
  /** Epoch seconds up to which range bars are approximate, or null. */
  approxUntil: number | null;
  /** Tick size of the instrument, or null when it is not known (a range can only be written in price). */
  tickSize: number | null;
  notice: ChartNotice | null;
  error: LiveError | null;
}

export const MAX_BARS = 2000;

export function initialState(symbol: string, timeframe: Timeframe): LiveState {
  return {
    symbol, timeframe, ready: false, link: false, bars: [], name: symbol, kind: "other", exchange: "", currency: "", timezone: "UTC",
    assetClass: null, fallback: false, source: "", dataState: "delayed", status: "live", delaySeconds: null, history: null, price: null,
    previousClose: null, change: null, changePct: null, openingRange: null, marketOpen: true, lastTsMs: null,
    interval: null, exactFrom: null, approxUntil: null, tickSize: null, notice: null, error: null,
  };
}

/** Messages of `/ws/market` that belong to one subscription. Every one carries `seq`, `epoch`, `source` and `data_state`. */
export interface SubMessage {
  type: string;
  sub?: string | null;
  seq?: number;
  epoch?: string;
  source?: string;
  data_state?: string;
  [field: string]: unknown;
}

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const dataStateOf = (value: unknown): DataState => (value === "live" ? "live" : "delayed"); // anything unclear is NOT live

function toCandle(value: unknown): Candle | null {
  if (!value || typeof value !== "object") return null;
  const c = value as Record<string, unknown>;
  if (!["t", "o", "h", "l", "c", "v"].every((key) => typeof c[key] === "number")) return null;
  const candle: Candle = { t: c.t as number, o: c.o as number, h: c.h as number, l: c.l as number, c: c.c as number, v: c.v as number };
  if (typeof c.i === "number" && Number.isFinite(c.i)) candle.i = c.i;
  if (c.approx === true) candle.approx = true;
  return candle;
}

function intervalOf(value: unknown): ProtocolInterval | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.kind === "time" && num(v.seconds) !== null) return { kind: "time", seconds: v.seconds as number };
  if (v.kind === "range" && num(v.size) !== null && (v.unit === "t" || v.unit === "p")) {
    return { kind: "range", size: v.size as number, unit: v.unit, tick_size: num(v.tick_size), ...(v.rounded === true ? { rounded: true } : {}) };
  }
  return null;
}

/** The warning pill for what the state holds now. */
export function noticeOf(state: Pick<LiveState, "interval" | "exactFrom" | "approxUntil" | "bars">): ChartNotice | null {
  const interval = state.interval;
  if (!interval) return null;
  if (interval.kind === "time") {
    return interval.seconds < 60 && state.exactFrom !== null ? { kind: "seconds_start", time: state.exactFrom } : null;
  }
  if (state.exactFrom === null) return { kind: "approx_all" };
  if (state.approxUntil === null) return null;
  // Once the trades cover every bar Hokma holds, nothing approximate is left to warn about.
  const until = state.approxUntil;
  return state.bars.some((bar) => bar.approx === true || bar.t < until) ? { kind: "approx_before", time: until } : null;
}

export function applySnapshot(state: LiveState, m: SubMessage): LiveState {
  const bars = Array.isArray(m.bars) ? (m.bars as unknown[]).map(toCandle).filter((bar): bar is Candle => bar !== null) : [];
  const next: LiveState = {
    ...state,
    ready: true,
    error: null,
    bars,
    name: typeof m.name === "string" && m.name ? m.name : state.symbol,
    kind: (typeof m.kind === "string" ? m.kind : "other") as AssetKind,
    exchange: typeof m.exchange === "string" ? m.exchange : "",
    currency: typeof m.currency === "string" ? m.currency : "",
    timezone: typeof m.timezone === "string" ? m.timezone : "UTC",
    assetClass: typeof m.asset_class === "string" ? m.asset_class : null,
    fallback: m.fallback === true,
    source: typeof m.source === "string" ? m.source : "",
    dataState: dataStateOf(m.data_state),
    delaySeconds: num(m.delay_seconds),
    history: historyOf(m.history),
    price: num(m.last),
    previousClose: num(m.previous_close),
    change: num(m.change),
    changePct: num(m.change_pct),
    openingRange: (m.opening_range as OpeningRange | null | undefined) ?? null,
    marketOpen: m.market_open !== false,
    lastTsMs: bars.length ? bars[bars.length - 1]!.t * 1000 : null,
    interval: intervalOf(m.interval),
    exactFrom: num(m.exact_from),
    approxUntil: num(m.approx_until),
    tickSize: num(m.tick_size),
  };
  return { ...next, notice: noticeOf(next) };
}

function withPrice(state: LiveState, m: SubMessage): LiveState {
  return {
    ...state,
    price: num(m.price) ?? state.price,
    change: m.change === undefined ? state.change : num(m.change),
    changePct: m.change_pct === undefined ? state.changePct : num(m.change_pct),
    lastTsMs: num(m.ts_ms) ?? state.lastTsMs,
    source: typeof m.source === "string" ? m.source : state.source,
    dataState: dataStateOf(m.data_state),
  };
}

/** Where a bar goes: by its index for range bars (several can start in the same second), by its start otherwise. */
function placeBar(bars: Candle[], bar: Candle): Candle[] | null {
  const next = bars.slice();
  const last = next[next.length - 1];
  const byIndex = typeof bar.i === "number";
  const keyOf = (b: Candle) => (byIndex ? (b.i ?? Number.NEGATIVE_INFINITY) : b.t);
  const key = keyOf(bar);
  if (!last || key > keyOf(last)) {
    next.push(bar);
    return next;
  }
  const at = next.findIndex((b) => keyOf(b) === key);
  if (at < 0) return null; // an older bar that is not in view: nothing to redraw
  next[at] = bar;
  return next;
}

/**
 * A bar replaces the one with the same start (time) or the same index (range), or is added at the end. In range the
 * bar in progress arrives with `final:false` and replaces the last one; when it closes (`final:true`) the next index
 * opens a new bar.
 */
export function applyBar(state: LiveState, m: SubMessage): LiveState {
  const bar = toCandle(m.bar);
  const next = withPrice(state, m);
  if (!bar) return next;
  const placed = placeBar(state.bars, bar);
  if (!placed) return next;
  const opening = m.opening_range as OpeningRange | undefined;
  const bars = placed.length > MAX_BARS ? placed.slice(-MAX_BARS) : placed;
  const result: LiveState = { ...next, bars, openingRange: opening ?? state.openingRange };
  return { ...result, notice: noticeOf(result) };
}

export function applyLast(state: LiveState, m: SubMessage): LiveState {
  return withPrice(state, m);
}

export function applyStatus(state: LiveState, m: SubMessage): LiveState {
  const code = m.state;
  const status: SubStatus = code === "live" || code === "delayed" || code === "reconnecting" || code === "closed" ? code : "reconnecting";
  return { ...state, status, dataState: dataStateOf(m.data_state), marketOpen: status !== "closed" };
}

/** The error of a subscription, with `params` when the engine sends them (it may not yet: then they are empty). */
export function errorOf(m: SubMessage): LiveError {
  const params: Record<string, string | number> = {};
  if (m.params && typeof m.params === "object" && !Array.isArray(m.params)) {
    for (const [key, value] of Object.entries(m.params as Record<string, unknown>)) {
      if (typeof value === "string" || (typeof value === "number" && Number.isFinite(value))) params[key] = value;
    }
  }
  return { code: String(m.code ?? "market.down"), message: String(m.message ?? ""), params };
}

type Translate = (key: string, params?: Params) => string;
const INTERVAL_PARAMS = ["interval", "base", "source", "symbol"] as const;

/** What the error panel says: the catalog text with its values, or the plain catalog or server text. */
export function errorText(error: LiveError, t: Translate = translate): string {
  if (error.code === "market.interval_unavailable") {
    if (INTERVAL_PARAMS.every((key) => key in error.params)) return t("market.err.interval_unavailable", error.params);
    return error.message || t("chart.interval.unavailable");
  }
  if (error.code === "market.seconds_need_ticks") return t("market.err.seconds_need_ticks");
  return codeText(error.code, error.message);
}

/**
 * Why seconds cannot be drawn for this symbol, for the interval box (`secondsDisabledReason`): the delayed fallback
 * only gives 1-minute bars. Null when nothing says so.
 */
export function secondsDisabledReason(state: LiveState, t: Translate = translate): string | null {
  if (state.error?.code === "market.seconds_need_ticks" || (state.ready && state.fallback)) return t("market.err.seconds_need_ticks");
  return null;
}

/** The note about the history: its source and delay. Null when the engine did not say it. */
function historyOf(raw: unknown): LiveState["history"] {
  if (typeof raw !== "object" || raw === null) return null;
  const h = raw as { source?: unknown; delay_seconds?: unknown };
  const delay = num(h.delay_seconds);
  return typeof h.source === "string" && h.source && delay !== null ? { source: h.source, delaySeconds: delay } : null;
}

/**
 * Minutes of delay of the history when the price runs live from a provider but the bars before the first live trade come
 * from a delayed source (Yahoo). Null when there is nothing to warn about.
 */
export function historyDelayMinutes(state: LiveState): number | null {
  const h = state.history;
  if (!h || state.fallback || h.delaySeconds <= 0 || state.dataState !== "live") return null;
  return Math.max(1, Math.round(h.delaySeconds / 60));
}

/** What the bar says. A reconnection or a closed market wins; otherwise `delayed` anywhere means delayed, never live. */
export function barMode(state: LiveState): BarMode {
  if (!state.link || state.status === "reconnecting") return "reconnecting";
  if (state.status === "closed") return "closed";
  if (state.dataState === "delayed" || state.status === "delayed") return "delayed";
  return "live";
}

export function delayMinutes(state: LiveState): number | null {
  return state.delaySeconds && state.delaySeconds > 0 ? Math.max(1, Math.round(state.delaySeconds / 60)) : null;
}

/** The shape the candle drawing and the PNG export already use. */
export function toChartData(state: LiveState): ChartData {
  const lastBar = state.bars[state.bars.length - 1];
  const lastMs = state.lastTsMs ?? (lastBar ? lastBar.t * 1000 : Date.now());
  return {
    symbol: state.symbol,
    name: state.name,
    kind: state.kind,
    exchange: state.exchange,
    currency: state.currency,
    timezone: state.timezone,
    timeframe: state.timeframe,
    interval: state.interval,
    candles: state.bars,
    last_price: state.price,
    previous_close: state.previousClose,
    change: state.change,
    change_pct: state.changePct,
    market_open: state.marketOpen,
    last_trade_at: Math.floor(lastMs / 1000),
    data_age_seconds: state.delaySeconds,
    delayed: state.dataState === "delayed",
    opening_range: state.openingRange,
    source: state.source,
    stale: false,
    fetched_at: Math.floor(lastMs / 1000),
  };
}
