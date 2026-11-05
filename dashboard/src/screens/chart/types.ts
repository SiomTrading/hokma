export type Timeframe = "1m" | "5m" | "15m" | "1h" | "1D";
export const TIMEFRAMES: readonly Timeframe[] = ["1m", "5m", "15m", "1h", "1D"];
export const DEFAULT_TIMEFRAME: Timeframe = "5m";

export type AssetKind = "equity" | "etf" | "index" | "future" | "crypto" | "currency" | "other";

export interface Candle {
  /** Start of the bar, epoch seconds UTC. */
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface OpeningRange {
  high: number;
  session_date: string;
  start: number;
  end: number;
  complete: boolean;
}

/** Response of GET /api/market/candles. */
export interface ChartData {
  symbol: string;
  name: string;
  kind: AssetKind;
  exchange: string;
  currency: string;
  timezone: string;
  timeframe: Timeframe;
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

export interface AssetHit {
  symbol: string;
  name: string;
  kind: AssetKind;
  exchange: string;
}

/** Response of GET /api/market/symbols. */
export interface AssetSearchResult {
  items: AssetHit[];
  suggested: boolean;
}
