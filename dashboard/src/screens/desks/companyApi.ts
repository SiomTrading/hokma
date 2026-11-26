import { api } from "@/api";

/** A block the provider could not fill: it says which one it asked and why, and carries no numbers. */
export interface NoData {
  ok: false;
  source: string;
  reason: string;
}
interface Filled {
  ok: true;
  source: string;
  fetched_at?: string | null;
}
export type Block<T> = NoData | (Filled & T);

export interface Ratios {
  market_cap_millions: number | null;
  beta: number | null;
  pe: number | null;
  eps: number | null;
  operating_margin: number | null;
  debt_to_equity: number | null;
  revenue_growth: number | null;
}
export interface Earning {
  date: string;
  hour?: string | null;
  eps_estimate?: number | null;
}
export interface QuarterResult {
  period: string;
  year?: number | null;
  quarter?: number | null;
  eps_estimate: number | null;
  eps_actual: number | null;
  surprise_pct: number | null;
}
export interface NewsItem {
  time: string | null;
  headline: string;
  source: string;
  url?: string | null;
}

/** The blocks of the Company panel of `GET /api/desks/{id}/company/{symbol}`. The price is not here: it comes from the stream. */
export interface CompanyData {
  symbol: string;
  profile: Block<{ name: string | null; exchange: string | null; industry: string | null; currency: string | null; market_cap_millions: number | null }>;
  fundamentals: Block<{ ratios: Ratios }>;
  events: {
    earnings: Block<{ next: Earning | null }>;
    expirations: Block<{ items: Array<{ date: string; days: number | null }> }>;
  };
  results: Block<{ rows: QuarterResult[] }>;
  news: Block<{ items: NewsItem[] }>;
}

export const companyApi = {
  get: (deskId: number, symbol: string) => api.get<CompanyData>(`/api/desks/${deskId}/company/${encodeURIComponent(symbol)}`),
};
