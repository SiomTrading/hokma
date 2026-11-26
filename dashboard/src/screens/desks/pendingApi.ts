import { api } from "@/api";

/** One order that waits for the owner, as `GET /api/desks/{id}/orders/pending` returns it. `order` is the Schwab order request (no account number in it). */
export interface PendingOrder {
  decision_id: number;
  /** Set when the order waits because a rule in notice mode tripped: it has its own three answers (Follow, No, Close). */
  proposal_id?: number;
  notice?: { rules: string[]; whys: string[]; timeout_min: number | null; stage: string } | null;
  action: string;
  agent: string | null;
  reason: string | null;
  why: string | null;
  order: PendingOrderBody | null;
  replaces_order_id: string | null;
  created_at: string;
  /** UTC, "YYYY-MM-DD HH:MM:SS". */
  expires_at: string;
}

export interface PendingOrderBody {
  orderType?: string;
  price?: number | string;
  orderStrategyType?: string;
  orderLegCollection?: Array<{ instruction?: string; quantity?: number; instrument?: { symbol?: string } }>;
  childOrderStrategies?: unknown[];
}

/** Answer of approving: sent, or approved but not sent (the new check did not pass) with the reason. Expiry arrives as a 409 error. */
export interface ApproveResult {
  status: "sent" | "replaced" | "canceled" | "not_sent";
  decision_id?: number;
  why?: string;
  order_id?: string | null;
}

export const pendingApi = {
  list: async (deskId: number): Promise<PendingOrder[]> =>
    (await api.get<{ pending: PendingOrder[] }>(`/api/desks/${deskId}/orders/pending`)).pending,
  approve: (deskId: number, decisionId: number) =>
    api.post<ApproveResult>(`/api/desks/${deskId}/orders/pending/${decisionId}/approve`, {}),
  reject: (deskId: number, decisionId: number) =>
    api.post<{ status: "rejected"; decision_id: number }>(`/api/desks/${deskId}/orders/pending/${decisionId}/reject`, {}),
};

/** "BUY 10 AAPL LIMIT 210.5", plus "+N legs" for a multi-leg order. Same idea as the backend summary line. */
export function summarizeOrder(order: PendingOrderBody | null): string {
  if (!order) return "";
  const legs = order.orderLegCollection ?? [];
  const first = legs[0] ?? {};
  const parts = [
    first.instruction ?? order.orderStrategyType ?? "",
    first.quantity === undefined ? "" : String(first.quantity),
    first.instrument?.symbol ?? "",
    order.orderType ?? "",
    order.price === undefined ? "" : String(order.price),
  ].filter((p) => p !== "");
  let text = parts.join(" ");
  if (legs.length > 1) text += ` +${legs.length - 1}`;
  return text;
}

/** Minutes left until `expiresAt` (UTC text), rounded up; 0 or less means expired. */
export function minutesLeft(expiresAt: string, now: number): number {
  const end = Date.parse(expiresAt.replace(" ", "T") + "Z");
  return Number.isNaN(end) ? 0 : Math.ceil((end - now) / 60000);
}
