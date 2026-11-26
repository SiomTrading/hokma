import { api } from "@/api";

export const VIEWS = ["very_bearish", "bearish", "neutral", "directional", "bullish", "very_bullish"] as const;
export type View = (typeof VIEWS)[number];
export const PRIORITIES = ["balanced", "max_return", "max_probability"] as const;
export type Priority = (typeof PRIORITIES)[number];

/** What the form sends. Empty optional fields go as null. */
export interface StrategiesAsk {
  symbol: string;
  view: View;
  target: number | null;
  budget: number | null;
  expiration: string | null;
  priority: Priority;
}

export interface Leg {
  position: string;
  kind: string;
  strike: number | null;
  premium: number;
  quantity?: number;
}

/** The verdict of a card: `verdict` is null when there is none and `reason` says why ("Sin veredicto: motivo"). */
export interface Verdict {
  verdict: "recommended" | "possible" | "not_worth" | "closed" | "only_if_shares" | "over_budget" | null;
  label: string | null;
  reason: string;
  backed_by: { source?: string; name?: string; seat?: string; model?: string } | null;
}

export interface Card {
  id: string;
  structure: string;
  family: string;
  legs: Leg[];
  net_premium_usd: number;
  cost_usd: number;
  credit_usd: number;
  capital_required_usd: number | null;
  max_profit_usd: number | null;
  max_loss_usd: number | null;
  unbounded_profit: boolean;
  unbounded_loss: boolean;
  breakevens: number[];
  p_profit: number | null;
  expected_value_usd: number | null;
  payoff: { spots: number[]; pnl_usd: number[] };
  status: "ok" | "locked" | "needs_shares" | "over_budget";
  status_message: string | null;
  locked: string | null;
  verdict: Verdict | null;
  can_open_detail: boolean;
}

/** The answer of `POST /api/desks/{id}/strategies`. When `ok` is false there are no cards and `message` says "Sin dato de {source}". */
export interface StrategiesResult {
  ok: boolean;
  symbol: string;
  message?: string;
  source?: string;
  expiration?: string;
  days?: number | null;
  spot?: number;
  as_of?: string | null;
  delayed?: boolean | null;
  notes?: unknown[];
  vol_view?: string;
  vol_view_source?: string;
  premium_selling?: { open: boolean; reason: string | null };
  reading?: string | null;
  model?: { used: boolean; agent: { name?: string } | null; reason: string | null };
  cards: Card[];
  skipped?: Array<{ structure: string; message: string }>;
}

/** The last consultation of the desk (kept by the server while it runs): the form and the cards, or nulls. */
export interface LastConsultation {
  form: { symbol: string; view: View; target: number | null; budget: number | null; expiration: string | null; priority: Priority } | null;
  result: StrategiesResult | null;
}

export interface ExpirationsResponse {
  expirations: Array<{ date: string; days: number }>;
}

export const strategiesApi = {
  ask: (deskId: number, body: StrategiesAsk) => api.post<StrategiesResult>(`/api/desks/${deskId}/strategies`, body),
  last: (deskId: number) => api.get<LastConsultation>(`/api/desks/${deskId}/strategies/last`),
  expirations: (symbol: string) => api.get<ExpirationsResponse>(`/api/market/options/expirations?symbol=${encodeURIComponent(symbol)}`),
};
