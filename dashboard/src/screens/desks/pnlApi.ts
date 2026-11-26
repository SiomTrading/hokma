import { api } from "@/api";

/** What the broker reports about one position. A field the broker does not give is null, never 0. */
export interface PnlPosition {
  symbol: string;
  qty: number | null;
  day_pl: number | null;
  open_pl: number | null;
}

/** The last good reading of an account (SPEC-026 v2, 4.1). `as_of` is the hour Hokma received it (ISO, UTC). */
export interface PnlLast {
  account_id: number;
  equity_now: number | null;
  equity_start: number | null;
  day_pnl: number | null;
  positions: PnlPosition[];
  as_of: string;
  source: string;
}

/** ok: a fresh reading. no_data: read before, nothing for 3 reads in a row. pending: not read yet. unavailable: that broker has no reader of P&L. */
export interface PnlView {
  state: "ok" | "no_data" | "pending" | "unavailable";
  source: string | null;
  since: string | null;
  last: PnlLast | null;
}

/** How often the screen asks. It reads what the engine already has: no broker is called. */
export const PNL_POLL_MS = 5000;

export const pnlApi = {
  /** By account id. A reply without the list (an older engine) is an empty one. */
  all: async (): Promise<Record<string, PnlView>> => (await api.get<{ accounts?: Record<string, PnlView> }>("/api/accounts/pnl")).accounts ?? {},
};
