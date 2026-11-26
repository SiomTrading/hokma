import { api } from "./client";
import type { CodeRow } from "./strategyCode";

export type BankOrigin = { kind: "hokma" } | { kind: "desk"; desk_id: number; desk_name: string | null };

/** One strategy of the bank with its last version (SPEC-030). */
export interface BankRow {
  bank_id: number;
  slug: string;
  title: string;
  line: string;
  status: string;
  version: number;
  source: { kind: string; reference: string | null };
  needs_new_rule: boolean;
  strategy_id: string | null;
  updated_at: string;
  origin: BankOrigin;
  agent_reviewed: boolean;
  owner_reviewed: boolean;
}

export interface BankPassage {
  passage_id: number;
  locator: string | null;
  body?: string;
}

export interface BankDetail {
  bank_id: number;
  slug: string;
  title: string;
  line: string;
  status: string;
  close_reason: string | null;
  version: {
    version: number;
    version_id: number;
    extraction: Record<string, unknown>;
    strategy_id: string | null;
    origin: BankOrigin;
    agent_reviewed: boolean;
    owner_reviewed: boolean;
  };
  source: { source_id: number; kind: string; reference: string | null; url: string | null; rights: string; cited: BankPassage[] };
  versions: Array<{ version: number; version_id: number; strategy_id: string | null; spec_version: number | null; needs_new_rule: boolean }>;
  events: Array<{ event_id: number; from: string | null; to: string; reason: string | null; actor: string; at: string }>;
}

/** The bank, read only. The code of a version and the owner's approval go through `strategyCodeApi`. */
export const strategyBankApi = {
  list: () => api.get<{ rows: BankRow[]; total: number }>("/api/strategy-bank"),
  read: (id: number, version?: number) => api.get<BankDetail>(`/api/strategy-bank/${id}${version ? `?version=${version}` : ""}`),
  code: (bankId: number) => api.get<{ rows: CodeRow[] }>(`/api/strategy-code?bank_id=${bankId}`),
};
