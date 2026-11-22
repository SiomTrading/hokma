/** Answers of GET /api/market/options/* (SPEC-014). A field that did not arrive is `null`, never zero. */

export interface UnderlyingBar {
  symbol: string;
  last: number | null;
  last_exchange: string | null;
  net_change: number | null;
  bid: number | null;
  bid_exchange: string | null;
  ask: number | null;
  ask_exchange: string | null;
  bid_size: number | null;
  ask_size: number | null;
  volume: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  as_of: string | null;
}

export interface ExpirationItem {
  date: string;
  days: number;
  multiplier: number;
}

export interface ExpirationsResponse {
  symbol: string;
  source: string;
  expirations: ExpirationItem[];
}

export interface OptionCell {
  symbol: string;
  itm: boolean;
  last: number | null;
  last_exchange: string | null;
  bid: number | null;
  bid_exchange: string | null;
  ask: number | null;
  ask_exchange: string | null;
  volume: number | null;
  open_interest: number | null;
  theta: number | null;
  delta: number | null;
  iv: number | null;
  intrinsic: number | null;
  prob_itm: number | null;
  quote_time: string | null;
  /** From the live chain (SPEC-016 4.3): false when the contract has no live data (the symbol limit of the source was reached). */
  live?: boolean;
  mark?: number | null;
  gamma?: number | null;
  vega?: number | null;
  rho?: number | null;
}

export interface OptionRow {
  strike: number;
  call: OptionCell | null;
  put: OptionCell | null;
}

export interface ExpirationGroup {
  date: string;
  days: number;
  multiplier: number;
  iv: number | null;
  expected_move: number | null;
  rows: OptionRow[];
}

export type PriceFeed = "indicative" | "opra" | "schwab";

export interface ChainResponse {
  underlying: string;
  underlying_price: number | null;
  source: string;
  price_feed: PriceFeed;
  greeks: "estimated" | "calculated" | "provider";
  market_open: boolean | null;
  as_of: string;
  notes: string[];
  expirations: ExpirationGroup[];
}

/** The strikes the student can ask for: a number of strikes around the price, or all of them. */
export const STRIKE_CHOICES = ["4", "6", "8", "10", "12", "14", "16", "20", "30", "50", "all"] as const;
export type StrikeChoice = (typeof STRIKE_CHOICES)[number];
export const DEFAULT_STRIKES: StrikeChoice = "14";
/** Calls and puts of one chain call: the engine takes at most this many expirations. */
export const MAX_PER_CALL = 4;
/** Groups that open by themselves when the chain opens. */
export const AUTO_OPEN = 3;
