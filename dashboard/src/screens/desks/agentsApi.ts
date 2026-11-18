import { api, ApiError } from "@/api";
import type { AgentsData } from "../agents/types";
import type { BudgetState, DeskModel, DeskRole } from "./types";

/** Where an agent stands inside one desk. `working` has a task running for this desk. */
export type AgentState = "working" | "idle" | "off";

/** One line of the activity list of an agent. */
export interface AgentActivity {
  at: string | null;
  text: string;
}

/** One member of a desk (`GET /api/desks/{id}/agents`). */
export interface DeskAgent {
  slug: string;
  name: string;
  role: DeskRole | string;
  /** Message keys, or plain text for agents the student made. Empty when the server sent none. */
  can: string[];
  cannot: string[];
  model: DeskModel;
  active: boolean;
  state: AgentState;
  tokens_week: number;
  last_action: string | null;
  activity: AgentActivity[];
}

/** Weekly use of one desk (`GET /api/desks/{id}/usage`). */
export interface DeskUsage {
  token_budget: number | null;
  used: number;
  pct: number | null;
  state: BudgetState;
}

interface RawAgent extends Partial<Omit<DeskAgent, "can" | "cannot" | "activity">> {
  /** The server names the agent `agent_slug` and its state `status`; the short names are read too. */
  agent_slug?: string;
  status?: AgentState;
  can?: string | string[];
  cannot?: string | string[];
  activity?: Array<string | { at?: string | null; text?: string }>;
}

/** The server's own keys of a role (`desks.role.chief.can`) are not in this catalog: the detail then uses its per-role texts. */
const list = (value: string | string[] | undefined): string[] =>
  (Array.isArray(value) ? value : value ? [value] : []).filter((entry) => !entry.startsWith("desks.role."));

function readAgent(raw: RawAgent): DeskAgent {
  const active = raw.active !== false;
  const reported = raw.status ?? raw.state;
  const state: AgentState = !active ? "off" : reported === "working" ? "working" : reported === "off" ? "off" : "idle";
  const slug = raw.agent_slug ?? raw.slug ?? "";
  return {
    slug,
    name: raw.name ?? slug,
    role: raw.role ?? "custom",
    can: list(raw.can),
    cannot: list(raw.cannot),
    model: raw.model ?? "sonnet",
    active,
    state,
    tokens_week: raw.tokens_week ?? 0,
    last_action: raw.last_action ?? null,
    activity: (raw.activity ?? []).map((item) => (typeof item === "string" ? { at: null, text: item } : { at: item.at ?? null, text: item.text ?? "" })),
  };
}

const base = (id: number) => `/api/desks/${id}`;

/** Team of one desk. The agent itself is global: bringing, changing the model or removing only touches this desk. */
export const deskAgentsApi = {
  list: async (id: number): Promise<DeskAgent[]> => {
    const body = await api.get<RawAgent[] | { agents: RawAgent[] }>(`${base(id)}/agents`);
    return (Array.isArray(body) ? body : (body.agents ?? [])).map(readAgent);
  },
  usage: async (id: number): Promise<DeskUsage> => {
    const raw = await api.get<Partial<DeskUsage>>(`${base(id)}/usage`);
    return { token_budget: raw.token_budget ?? null, used: raw.used ?? 0, pct: raw.pct ?? null, state: raw.state ?? "ok" };
  },
  /** Every agent of Hokma, to pick one to bring. */
  catalog: (): Promise<AgentsData> => api.get<AgentsData>("/api/agents"),
  add: (id: number, body: { agent_slug: string; model: DeskModel; role?: DeskRole }) => api.post<unknown>(`${base(id)}/agents`, body),
  update: (id: number, slug: string, patch: { model?: DeskModel; active?: boolean }) =>
    api.put<unknown>(`${base(id)}/agents/${encodeURIComponent(slug)}`, patch),
  remove: (id: number, slug: string) => api.delete<unknown>(`${base(id)}/agents/${encodeURIComponent(slug)}`),
};

/** The server's stable code of a failed call, or an empty string. */
export function errorCode(cause: unknown): string {
  return cause instanceof ApiError ? cause.code : "";
}
