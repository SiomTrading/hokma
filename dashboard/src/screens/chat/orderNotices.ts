import { api } from "@/api";
import type { ServerEvent } from "./types";

/** An order that waits for the owner because a rule in notice mode tripped (SPEC-026, 3390b). It carries no token: the chat answers through the routes of the app. */
export interface OrderNotice {
  proposal_id: number;
  desk_id: number;
  desk: string;
  account: string;
  rules: string[];
  whys: string[];
  timeout_min: number | null;
  /** UTC, "YYYY-MM-DD HH:MM:SS". When it passes the order is NOT executed. */
  expires_at: string | null;
  stage: string;
  order: string;
  summary?: string;
}

/** What Close would do, to be read before confirming. */
export interface ClosePlan {
  status: string;
  proposal_id: number;
  plan_token: string;
  text: string;
}

/** A rule in notice mode reached its cap with no order in the way (SPEC-026, 3390e4). It has its own id: it is not an order. Numbers come formatted from the server. */
export interface CapNotice {
  cap_id: number;
  account_id: number;
  account: string;
  desk: string;
  rule_id: string;
  control: string;
  metric: string;
  value: string;
  cap: string;
  read_at: string | null;
  timeout_min: number;
  /** UTC, "YYYY-MM-DD HH:MM:SS". When it passes nothing is done: the notice only stays recorded. */
  expires_at: string | null;
  stage: string;
}

export type NoticeScope = "order" | "day";

export const capApi = {
  list: async (): Promise<CapNotice[]> => {
    const body = await api.get<{ notices?: CapNotice[] }>("/api/orders/cap-notices");
    return Array.isArray(body?.notices) ? body.notices : [];
  },
  follow: (id: number) => api.post<{ status: string }>(`/api/orders/cap-notices/${id}/follow`, {}),
  block: (id: number) => api.post<{ status: string }>(`/api/orders/cap-notices/${id}/block`, {}),
  /** First step of Close positions: nothing is closed; it answers with the list of what would be closed and what stays. */
  close: (id: number) => api.post<{ status: string; plan_token: string; text: string }>(`/api/orders/cap-notices/${id}/close`, {}),
  confirmClose: (id: number, planToken: string) => api.post<{ status: string }>(`/api/orders/cap-notices/${id}/close/confirm`, { plan_token: planToken }),
  back: (id: number) => api.post<{ status: string }>(`/api/orders/cap-notices/${id}/back`, {}),
};

export const noticeApi = {
  list: async (): Promise<OrderNotice[]> => {
    const body = await api.get<{ notices?: OrderNotice[] }>("/api/orders/notices");
    return Array.isArray(body?.notices) ? body.notices : [];
  },
  follow: (id: number, scope: NoticeScope) => api.post<{ status: string }>(`/api/orders/notices/${id}/follow`, { scope }),
  no: (id: number) => api.post<{ status: string }>(`/api/orders/notices/${id}/no`, {}),
  /** First step of Close: nothing is closed; it answers with the list of what would be closed and what stays, in the words of the owner. */
  close: (id: number) => api.post<ClosePlan>(`/api/orders/notices/${id}/close`, {}),
  /** Second step: closes exactly the list that was shown (its token goes back so a newer list is never closed by mistake). */
  confirmClose: (id: number, planToken: string) => api.post<{ status: string }>(`/api/orders/notices/${id}/close/confirm`, { plan_token: planToken }),
};

/** The notices that wait now, live: the cards of the chat and the amber of the minimized strip read it. */
export class NoticeStore {
  private items: OrderNotice[] = [];
  private caps: CapNotice[] = [];
  private readonly listeners = new Set<() => void>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get = (): OrderNotice[] => this.items;

  /** The notices of a cap that wait now (no order in the way). */
  getCaps = (): CapNotice[] => this.caps;

  /** How many notices wait for an answer, of orders and of caps: above 0 the minimized strip turns amber. */
  count = (): number => this.items.length + this.caps.length;

  private set(items: OrderNotice[]): void {
    this.items = items;
    this.listeners.forEach((listener) => listener());
  }

  private setCaps(caps: CapNotice[]): void {
    this.caps = caps;
    this.listeners.forEach((listener) => listener());
  }

  apply(event: ServerEvent): void {
    if (event.type === "order_notice") {
      const { type: _type, ...notice } = event;
      this.set([...this.items.filter((n) => n.proposal_id !== notice.proposal_id), notice]);
    } else if (event.type === "order_notice_closed") {
      if (this.items.some((n) => n.proposal_id === event.proposal_id)) this.set(this.items.filter((n) => n.proposal_id !== event.proposal_id));
    } else if (event.type === "cap_notice") {
      const { type: _type, ...cap } = event;
      this.setCaps([...this.caps.filter((n) => n.cap_id !== cap.cap_id), cap]);
    } else if (event.type === "cap_notice_closed") {
      if (this.caps.some((n) => n.cap_id === event.cap_id)) this.setCaps(this.caps.filter((n) => n.cap_id !== event.cap_id));
    }
  }

  /** The truth of the server: at start and every time the socket opens again, so a card sent while the chat was closed is not lost. */
  async refresh(): Promise<void> {
    try {
      this.set(await noticeApi.list());
    } catch {
      /* the next event or the next open tries again */
    }
    try {
      this.setCaps(await capApi.list());
    } catch {
      /* the same */
    }
  }
}
