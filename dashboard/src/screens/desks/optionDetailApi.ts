import { api } from "@/api";

/** What the panel keeps and sends: the request that makes a strategy. `structure` with its `strikes` (low to high), or `legs`. */
export interface DetailRequest {
  symbol: string;
  expiration: string;
  structure: string | null;
  strikes: number[] | null;
  legs: Array<Record<string, unknown>> | null;
  quantity: number;
}

/** The strategy the panel opens with (kept by the server): the request plus what the owner left on the sliders and its saved key. */
export interface Selection extends DetailRequest {
  range_pct: number | null;
  iv_mult: number | null;
  strategy_key: string | null;
  name: string | null;
}

export interface SavedStrategy extends DetailRequest {
  key: string;
  name: string;
  saved_at?: string;
}

export interface DetailLeg {
  instrument: "option" | "stock";
  position: "long" | "short";
  kind?: "call" | "put";
  strike?: number;
  premium: number;
  quantity: number;
  iv?: number | null;
  iv_used?: number | null;
}

export interface Metrics {
  net_premium_usd: number;
  premium_type: "debit" | "credit" | "even";
  max_profit_usd: number | null;
  unbounded_profit: boolean;
  max_loss_usd: number | null;
  unbounded_loss: boolean;
  breakevens: number[];
  p_profit: number | null;
  expected_value_usd: number | null;
}

export interface EventBlock {
  kind?: string;
  ok?: boolean;
  source?: string | null;
  date: string | null;
  days_from_today?: number | null;
  before_expiration?: boolean;
  relation?: "before_expiration" | "after_expiration" | "none" | "no_data";
  message: string;
  model_ignores_dividend?: boolean;
}

/** The answer of `POST /api/desks/{id}/option-detail`. When `ok` is false `message` says "Sin dato de {fuente}" and nothing else is drawn. */
export interface DetailResult {
  ok: boolean;
  symbol: string;
  expiration: string;
  message?: string;
  source?: string;
  days?: number;
  spot?: number;
  as_of?: string | null;
  delayed?: boolean | null;
  structure?: string | null;
  legs?: DetailLeg[];
  metrics?: Metrics;
  table?: {
    rows: Array<{ pct: number; price: number; current: boolean }>;
    columns: Array<{ days: number; date: string; expiration: boolean }>;
    pnl_usd: Array<Array<number | null>>;
    spot_row: number;
    range_pct: number;
  };
  curves?: {
    spots: number[];
    expiration_pnl_usd: number[];
    mid_days: number;
    mid_pnl_usd: number[] | null;
    mid_breakevens: number[] | null;
    mid_breakevens_approximate: boolean;
    mid_breakevens_in_range_only: boolean;
  };
  breakevens?: number[];
  model?: { iv_mult: number; iv_mean: number | null };
  events?: { expiration: { date: string; days: number }; earnings: EventBlock; ex_dividend: EventBlock };
  recalc?: { price_move_pct: number; debounce_ms: number; by_event_only: boolean; anchor_spot: number };
  limits?: { range_pct: { min: number; max: number; default: number }; iv_mult: { min: number; max: number; default: number } };
  warnings?: string[];
  notes?: unknown[];
}

export interface Account {
  id: number;
  name: string;
  broker: string;
  mask: string | null;
}

/** The answer of the proposal. `needs_account` records nothing: the screen asks which account (or opens Add account). */
export interface ProposeResult {
  status: "needs_account" | "no_data" | "refused" | "analysis_only" | "awaiting_owner" | "sent" | "filled" | "partially_filled" | "rejected_by_broker" | "expired" | "canceled";
  code: string;
  why: string;
  strategy_key?: string;
  structure?: string;
  accounts?: Account[];
  proposal_id?: number;
  locked?: string;
}

export const PRINCIPAL = "principal";
export const DEBOUNCE_MS = 150;

export const detailApi = {
  ask: (deskId: number, body: DetailRequest & { range_pct: number; iv_mult: number }) => api.post<DetailResult>(`/api/desks/${deskId}/option-detail`, body),
  selection: async (deskId: number): Promise<Selection | null> => (await api.get<{ selection: Selection | null }>(`/api/desks/${deskId}/option-detail/selection`)).selection,
  remember: (deskId: number, body: DetailRequest & { range_pct?: number; iv_mult?: number; strategy_key?: string | null; name?: string | null }) =>
    api.put<{ selection: Selection }>(`/api/desks/${deskId}/option-detail/selection`, body),
  saved: async (deskId: number): Promise<SavedStrategy[]> => (await api.get<{ strategies: SavedStrategy[] }>(`/api/desks/${deskId}/option-detail/saved`)).strategies,
  save: (deskId: number, name: string, body: DetailRequest) => api.post<SavedStrategy>(`/api/desks/${deskId}/option-detail/saved`, { name, ...body }),
  forget: async (deskId: number, key: string): Promise<SavedStrategy[]> =>
    (await api.delete<{ strategies: SavedStrategy[] }>(`/api/desks/${deskId}/option-detail/saved/${encodeURIComponent(key)}`)).strategies,
  propose: (deskId: number, body: DetailRequest & { strategy_key: string }) => api.post<ProposeResult>(`/api/desks/${deskId}/option-detail/propose`, body),
  bind: (deskId: number, strategyKey: string, accountId: number) =>
    api.put<unknown>(`/api/desks/${deskId}/strategies/${encodeURIComponent(strategyKey)}/account`, { account_id: accountId }),
};

/**
 * The rule of the recalculation by price (the server sends the percent): true when the live price moved more than `pct` percent from the price of the
 * answer the panel holds. An event of the stream calls it, never a clock.
 */
export function movedEnough(anchor: number | null | undefined, live: number | null | undefined, pct: number): boolean {
  if (anchor == null || live == null || !(anchor > 0) || !Number.isFinite(live)) return false;
  return (Math.abs(live - anchor) / anchor) * 100 > pct;
}

/** The request of a card of the Strategies panel: the structure with its strikes, low to high, or its legs when the strikes do not fit the structure. */
export function requestOfCard(symbol: string, expiration: string, card: { structure: string; legs: Array<{ position: string; kind: string; strike: number | null; quantity?: number }> }, count: Record<string, number>): DetailRequest {
  const strikes = [...new Set(card.legs.filter((l) => l.strike != null).map((l) => l.strike as number))].sort((a, b) => a - b);
  const needs = count[card.structure];
  if (needs !== undefined && strikes.length === needs) return { symbol, expiration, structure: card.structure, strikes, legs: null, quantity: 1 };
  const legs = card.legs.map((l) => (l.kind === "stock" ? { instrument: "stock", position: l.position, quantity: l.quantity } : { position: l.position, kind: l.kind, strike: l.strike }));
  return { symbol, expiration, structure: null, strikes: null, legs, quantity: 1 };
}

/** Strikes each structure of the catalog needs (the same numbers as `quant/options/structures.py`). */
export const STRIKES_OF: Record<string, number> = {
  long_call: 1, long_put: 1, debit_call_spread: 2, debit_put_spread: 2, short_put: 1, short_call: 1, credit_call_spread: 2, credit_put_spread: 2, iron_condor: 4,
  short_straddle: 1, short_strangle: 2, covered_call: 1, cash_secured_put: 1, iron_butterfly: 3, protective_put: 1,
};
