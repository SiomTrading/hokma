import { api } from "@/api";

export interface Broker {
  id: number;
  name: string;
  kind: string;
  commission: { type: string; value: number; min: number };
  slippage_bps: number;
  /** What the broker can do. Optional: a broker from an older engine has none. */
  capabilities?: { accounts: boolean; reset: boolean; orders: boolean; positions: boolean };
  /** Agents may send orders through this broker (`brokers.config.orders_enabled`). Off unless the owner turned it on. */
  orders_enabled?: boolean;
}

/** One account as `GET /api/brokers/{id}/accounts` returns it. `cash` and `equity` are null when the broker cannot tell. */
export interface Account {
  id: number;
  broker_id: number;
  broker: string;
  name: string;
  initial_capital: number;
  currency: string;
  status: string;
  /** Every desk that holds the account (it may be in several, or in none). */
  desk_ids: number[];
  cash: number | null;
  equity: number | null;
  as_of: string;
  /** False for accounts of a real broker (Schwab): they are never reset. */
  can_reset?: boolean;
  /** Masked number (••••1234) or null. It is the only identity of a real account. */
  mask?: string | null;
}

/** A broker of real money (Schwab): its accounts come from its connection, cannot be created or reset here, and it has the orders switch. */
export const isRealBroker = (broker: Broker): boolean => broker.kind !== "sim";

export const accountsApi = {
  brokers: async (): Promise<Broker[]> => (await api.get<{ brokers: Broker[] }>("/api/brokers")).brokers,
  accounts: async (brokerId: number): Promise<Account[]> =>
    (await api.get<{ accounts: Account[] }>(`/api/brokers/${brokerId}/accounts`)).accounts,
  updateCosts: (brokerId: number, commissionBps: number, slippageBps: number) =>
    api.put<Broker>(`/api/brokers/${brokerId}/costs`, { commission: { type: "bps", value: commissionBps, min: 0 }, slippage_bps: slippageBps }),
  setOrdersEnabled: (brokerId: number, enabled: boolean) => api.put<Broker>(`/api/brokers/${brokerId}/orders-enabled`, { enabled }),
  createAccount: (brokerId: number, name: string, capital: number) =>
    api.post<Account>(`/api/brokers/${brokerId}/accounts`, { name, capital }),
  reset: (accountId: number) => api.post<Account>(`/api/accounts/${accountId}/reset`, { confirmed: true }),
  /** Puts an account that exists in a desk. The first account of a desk also becomes the account of its main strategy. */
  addToDesk: (deskId: number, accountId: number) => api.post<Account>(`/api/desks/${deskId}/accounts`, { account_id: accountId }),
  /** Creates a demo account and puts it in the desk, in one step: if anything fails nothing is left. */
  addDemoToDesk: (deskId: number, name: string, initialCapital: number) =>
    api.post<Account>(`/api/desks/${deskId}/accounts`, { demo: { name, initial_capital: initialCapital } }),
  removeFromDesk: (deskId: number, accountId: number) =>
    api.delete<{ desk_id: number; account_id: number; accounts: number[] }>(`/api/desks/${deskId}/accounts/${accountId}`),
};
