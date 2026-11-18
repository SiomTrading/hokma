import { api } from "@/api";
import type { CreateDeskPayload, DeskList, DeskSummary, DeskTemplateDef } from "./types";

/** The server names a template `key`, its agents `agent_slug` and its cap `suggested_budget`; older shapes are read too. */
interface RawTemplate {
  id?: string;
  key?: string;
  token_budget?: number | null;
  suggested_budget?: number | null;
  agents?: Array<{ slug?: string; agent_slug?: string; role: string; model: string }>;
}

function readTemplate(raw: RawTemplate) {
  return {
    id: raw.id ?? raw.key ?? "",
    token_budget: raw.token_budget ?? raw.suggested_budget ?? null,
    agents: (raw.agents ?? []).map((agent) => ({ slug: agent.slug ?? agent.agent_slug ?? "", role: agent.role, model: agent.model })),
  };
}

export const desksApi = {
  /** The contract does not fix the envelope of the list, so a bare array and `{ desks: [...] }` are both read. */
  list: async (): Promise<DeskSummary[]> => {
    const body = await api.get<DeskList | DeskSummary[]>("/api/desks");
    return Array.isArray(body) ? body : (body.desks ?? []);
  },
  /** Templates of the wizard: roles, agent and default model of each, and the suggested budget. */
  templates: async (): Promise<DeskTemplateDef[]> => {
    const body = await api.get<RawTemplate[] | { templates: RawTemplate[] }>("/api/desks/templates");
    const raw = Array.isArray(body) ? body : (body.templates ?? []);
    return raw.map(readTemplate).filter((item) => item.id !== "custom") as DeskTemplateDef[];
  },
  /** Creates the desk. The answer carries at least the new `id`. */
  create: (payload: CreateDeskPayload) => api.post<{ id: number }>("/api/desks", payload),
  /** Deletes the desk: its folder goes to the Trash and can be restored. Agents and conversations stay. */
  remove: (id: number) => api.delete<{ id: number; trash_id: number | null }>(`/api/desks/${id}`),
};
