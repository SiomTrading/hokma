/** Wire types of the chat engine and of the conversation REST API. */

export const MODELS = ["haiku", "sonnet", "opus"] as const;
export type ModelId = (typeof MODELS)[number];
export const DEFAULT_MODEL: ModelId = "sonnet";

export type SummaryKind = "write_file" | "edit_file" | "read_file" | "run_command" | "fetch_url" | "search" | "other";
export interface ToolSummary {
  kind: SummaryKind;
  target: string;
}

export interface ProjectRef {
  id: string;
  name: string;
}

export interface Usage {
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  cost_usd: number;
}

export interface PermissionBody {
  conversation_id?: string;
  turn_id: string;
  request_id: string;
  tool_use_id: string;
  tool: string;
  input: Record<string, unknown>;
  summary: ToolSummary;
  expires_at: number;
}

/** A tool line saved with an answer. `at` is how many characters of the answer came before the tool call. */
export interface StoredTool {
  id: string;
  name: string;
  kind: SummaryKind;
  target: string;
  at: number;
  is_error: boolean | null;
  result: string | null;
}

/** A refusal of one of the always-on protections, as the server sends it live and as it is saved with the answer. */
export interface GuardBlock {
  id: number | string;
  rule: string;
  tool: string;
  target: string;
  at: string;
}

export interface StoredAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "file";
}

/** The answer of a turn that is still running, for a socket that reconnects in the middle of it. */
export interface PartialTurn {
  turn_id: string;
  text: string;
  tools: StoredTool[];
}

export interface HistoryMessage {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  status: "complete" | "stopped" | "error";
  tokens_in: number;
  tokens_out: number;
  created_at: number;
  tools?: StoredTool[];
  attachments?: StoredAttachment[];
  blocks?: GuardBlock[];
}

/** Everything the server can send on /ws/chat. */
export type ServerEvent =
  | { type: "conversation"; conversation_id: string; title: string; model: string; agent: string | null; project: ProjectRef | null; created: boolean }
  | { type: "turn_start"; conversation_id: string; turn_id: string; message_id: string }
  | { type: "delta"; conversation_id: string; turn_id: string; text: string }
  | { type: "tool_use"; conversation_id: string; turn_id: string; tool_use_id: string; name: string; input: Record<string, unknown>; summary: ToolSummary }
  | { type: "tool_result"; conversation_id: string; turn_id: string; tool_use_id: string; is_error: boolean; text: string }
  | ({ type: "permission_request" } & PermissionBody)
  | { type: "permission_resolved"; conversation_id: string; request_id: string; decision: "allow" | "deny"; by: "human" | "timeout" | "stop" | "engine" }
  | ({ type: "guard_block"; conversation_id: string; turn_id?: string } & GuardBlock)
  | { type: "done"; conversation_id: string; turn_id: string; message_id: string | null; stopped: boolean; usage: Usage; engine_session: string | null }
  | { type: "error"; conversation_id?: string; turn_id?: string; code: string; message: string }
  | { type: "command_result"; conversation_id?: string; name: "model" | "agent" | "project"; ok: boolean; value: unknown; options?: string[] }
  | { type: "history"; conversation_id: string; messages: HistoryMessage[] }
  | { type: "state"; conversation_id: string; state: "idle" | "running"; pending_permissions: PermissionBody[]; partial?: PartialTurn }
  | { type: "pong" };

export interface ConversationSummary {
  id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  model: string | null;
  agent: string | null;
  project_id: string | null;
  message_count: number;
  started_at: string | null;
  last_message_at: string | null;
}

export interface ChatOptions {
  agents: Array<{ slug: string; name: string; description: string }>;
  projects: ProjectRef[];
  default_project: ProjectRef | null;
}

export interface UploadedAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "file";
}

export function isModel(value: unknown): value is ModelId {
  return typeof value === "string" && (MODELS as readonly string[]).includes(value);
}
