import { api } from "./client";

/** A strategy written as code, as the chat notice shows it (SPEC-031 A2). */
export interface CodeRow {
  rule_version_id: number;
  rule_id: number;
  bank_id: number;
  slug: string;
  title: string;
  version: number;
  registry_id: string;
  stage: "draft" | "failed" | "generated" | "owner_reviewed" | "rejected" | "retired";
  origin: { kind: "hokma" } | { kind: "desk"; desk_id: number; desk_name: string | null };
  summary: string | null;
  tests: number;
  reviewer: string | null;
}

export interface CodeDetail {
  registry_id: string;
  code: string;
  stage: string;
  agent_reviewed: boolean;
  owner_reviewed: boolean;
}

/** The owner's door for the strategies that the code reviewer approved. Approve and reject send the confirmation of the owner's tab like every call that decides (the client adds it). */
export const strategyCodeApi = {
  generated: () => api.get<{ rows: CodeRow[] }>("/api/strategy-code?stage=generated"),
  read: (id: number) => api.get<CodeDetail>(`/api/strategy-code/${id}`),
  approve: (id: number) => api.post<{ status: string }>(`/api/strategy-code/${id}/approve`, { confirm: true }),
  reject: (id: number, note: string) => api.post<{ status: string }>(`/api/strategy-code/${id}/reject`, note ? { note } : {}),
};
