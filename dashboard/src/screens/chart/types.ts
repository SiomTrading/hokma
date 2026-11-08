/**
 * A canonical interval of the grammar in `intervals.ts`: time (`7m`, `15s`, `1D`) or range (`r:10t`, `r:2.5p`).
 * `TIMEFRAMES` are the default favorites; any other valid interval is accepted too.
 */
export type Timeframe = string;
export const TIMEFRAMES: readonly Timeframe[] = ["1m", "5m", "15m", "1h", "1D"];
export const DEFAULT_TIMEFRAME: Timeframe = "5m";

/** `interval` of a snapshot: what the engine builds for the subscription. */
export type ProtocolInterval =
  | { kind: "time"; seconds: number }
  | { kind: "range"; size: number; unit: "t" | "p"; tick_size: number | null; rounded?: boolean };

/** Asset classes of `asset_class` and `route`; `index` only exists as a search filter. */
export type AssetClassName = "equity" | "future" | "crypto" | "forex";
export type SearchClass = AssetClassName | "index";

export type AssetKind = "equity" | "etf" | "index" | "future" | "crypto" | "currency" | "other";

export interface Candle {
  /** Start of the bar, epoch seconds UTC. */
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  /** Range bars only: running index, unique and increasing; several bars can share the same `t`. */
  i?: number;
  /** Range bars only: built from 1-minute bars instead of trades. */
  approx?: boolean;
}

export interface OpeningRange {
  high: number;
  session_date: string;
  start: number;
  end: number;
  complete: boolean;
}

/** Response of GET /api/market/candles, and the shape the drawing and the PNG export use. */
export interface ChartData {
  symbol: string;
  name: string;
  kind: AssetKind;
  exchange: string;
  currency: string;
  timezone: string;
  timeframe: Timeframe;
  /** What the engine builds: time with its length, or range with its size. Null before the engine says it. */
  interval?: ProtocolInterval | null;
  candles: Candle[];
  last_price: number | null;
  previous_close: number | null;
  change: number | null;
  change_pct: number | null;
  market_open: boolean;
  last_trade_at: number | null;
  data_age_seconds: number | null;
  delayed: boolean;
  opening_range: OpeningRange | null;
  source: string;
  stale: boolean;
  fetched_at: number;
}

/** The provider that would serve a symbol today, worked out without connecting anything. */
export interface AssetRoute {
  provider: string;
  source: string;
  data_state: "live" | "delayed";
}

export interface AssetHit {
  symbol: string;
  name: string;
  kind: AssetKind;
  exchange: string;
  /** Absent while the engine does not send it. */
  asset_class?: AssetClassName | null;
  route?: AssetRoute | null;
}

/** Response of GET /api/market/symbols. */
export interface AssetSearchResult {
  items: AssetHit[];
  suggested: boolean;
  /** Classes with a live provider connected: the green dot of the class chips. */
  live_classes?: string[];
}
