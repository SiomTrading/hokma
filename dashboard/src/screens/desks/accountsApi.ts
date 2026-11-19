import { api } from "@/api";
import type { DeskAccount } from "./types";

export interface Broker {
  id: number;
  name: string;
  kind: string;
  commission: { type: string; value: number; min: number };
  slippage_bps: number;
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
  desk_id: number | null;
  cash: number | null;
  equity: number | null;
  as_of: string;
}

export const accountsApi = {
  brokers: async (): Promise<Broker[]> => (await api.get<{ brokers: Broker[] }>("/api/brokers")).brokers,
  accounts: async (brokerId: number): Promise<Account[]> =>
    (await api.get<{ accounts: Account[] }>(`/api/brokers/${brokerId}/accounts`)).accounts,
  updateCosts: (brokerId: number, commissionBps: number, slippageBps: number) =>
    api.put<Broker>(`/api/brokers/${brokerId}/costs`, { commission: { type: "bps", value: commissionBps, min: 0 }, slippage_bps: slippageBps }),
  createAccount: (brokerId: number, name: string, capital: number) =>
    api.post<Account>(`/api/brokers/${brokerId}/accounts`, { name, capital }),
  reset: (accountId: number) => api.post<Account>(`/api/accounts/${accountId}/reset`, { confirmed: true }),
  unassign: (deskId: number) => api.delete<{ desk_id: number; account: null }>(`/api/desks/${deskId}/account`),
  assign: (deskId: number, accountId: number) => api.put<DeskAccount>(`/api/desks/${deskId}/account`, { account_id: accountId }),
};
