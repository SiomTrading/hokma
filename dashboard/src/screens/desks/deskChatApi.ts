import { api, ApiError } from "@/api";
import type { DeskModel } from "./types";

/** The conversation of a desk: its project, its chief and the chief's model in this desk. */
export interface DeskConversation {
  id: string;
  /** Slug of the desk chief: the agent whose model the chat selector changes. */
  agent: string | null;
  model: DeskModel;
}

interface RawConversation {
  id?: string | number;
  conversation_id?: string | number;
  agent?: string | null;
  agent_slug?: string | null;
  model?: string | null;
  conversation?: RawConversation;
}

const isDeskModel = (value: unknown): value is DeskModel => value === "haiku" || value === "sonnet" || value === "opus";

/** Stable code the server uses when the weekly token budget of the desk is spent. */
export const BUDGET_EXHAUSTED = "desk_budget_exhausted";

export function readConversation(body: RawConversation): DeskConversation {
  const raw = body.conversation ?? body;
  // the desk API answers the database id as a number; the chat socket only knows conversation ids as text
  const id = String(raw.conversation_id ?? raw.id ?? "");
  return { id, agent: raw.agent_slug ?? raw.agent ?? null, model: isDeskModel(raw.model) ? raw.model : "sonnet" };
}

export const deskChatApi = {
  /** Returns the conversation of the desk, or creates it. Messages then go through the chat socket as in any chat. */
  conversation: async (deskId: number): Promise<DeskConversation> => {
    const body = await api.post<RawConversation>(`/api/desks/${deskId}/conversation`, {});
    const conversation = readConversation(body);
    if (!conversation.id) throw new ApiError(502, "desks.chat.no_conversation", "");
    return conversation;
  },
};
