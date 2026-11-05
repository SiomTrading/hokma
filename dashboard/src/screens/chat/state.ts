import { DEFAULT_MODEL, isModel } from "./types";
import { attachmentUrl } from "./chatApi";
import type { ModelId, PermissionBody, ProjectRef, ServerEvent, StoredAttachment, StoredTool, ToolSummary } from "./types";

export interface AttachmentChip {
  key: string;
  /** Id from POST /api/attachments, set when the upload ends. */
  id?: string;
  name: string;
  size: number;
  kind: "image" | "file";
  mime: string;
  /** Object URL for the thumbnail of images. Revoked when the chip is dropped. */
  previewUrl?: string;
  status: "uploading" | "ready" | "error";
  /** A voice note whose text was transcribed into the message. */
  transcribed?: boolean;
}

export type Item =
  | { kind: "user"; key: string; messageId?: string; text: string; attachments: AttachmentChip[] }
  | { kind: "assistant"; key: string; text: string; streaming: boolean; status: "complete" | "stopped" | "error"; messageId?: string }
  | { kind: "tool"; key: string; toolUseId: string; name: string; summary: ToolSummary; result?: { isError: boolean; text: string } }
  | { kind: "permission"; key: string; body: PermissionBody; sending?: boolean; resolution?: { decision: "allow" | "deny"; by: string } }
  | { kind: "notice"; key: string; tone: "info" | "warn" | "bad"; text: string };

export interface ChatState {
  id: string | null;
  title: string;
  model: ModelId;
  agent: string | null;
  project: ProjectRef | null;
  items: Item[];
  running: boolean;
  stopping: boolean;
  /** True from the moment a stored conversation is opened until its history arrives. */
  loading: boolean;
  loadError: string | null;
}

export type Action =
  | { type: "reset"; id: string | null }
  | { type: "local_send"; text: string; attachments: AttachmentChip[] }
  | { type: "local_stop" }
  | { type: "local_model"; model: ModelId }
  | { type: "local_project"; project: ProjectRef | null }
  | { type: "permission_sending"; requestId: string }
  | { type: "notice"; tone: "info" | "warn" | "bad"; text: string }
  | { type: "event"; event: ServerEvent; errorText?: (code: string) => string };

let counter = 0;
export function nextKey(prefix: string): string {
  counter += 1;
  return `${prefix}-${counter}`;
}

export function initialState(id: string | null): ChatState {
  return { id, title: "", model: DEFAULT_MODEL, agent: null, project: null, items: [], running: false, stopping: false, loading: id !== null, loadError: null };
}

function closeStreaming(items: Item[], status: "complete" | "stopped" | "error" = "complete"): Item[] {
  return items.map((item) => (item.kind === "assistant" && item.streaming ? { ...item, streaming: false, status } : item));
}

function chipOf(file: StoredAttachment): AttachmentChip {
  return {
    key: `att-${file.id}`,
    id: file.id,
    name: file.name,
    size: file.size,
    kind: file.kind,
    mime: file.mime,
    previewUrl: file.kind === "image" ? attachmentUrl(file.id) : undefined,
    status: "ready",
  };
}

/**
 * Items of one answer: the text cut at the points where the tools were called, so a saved answer looks as it did
 * while it ran. `running` is a turn still in progress: its last piece keeps streaming and a tool with no result yet
 * is still running.
 */
function answerItems(base: string, text: string, tools: StoredTool[], status: "complete" | "stopped" | "error", running: boolean, messageId?: string): Item[] {
  const items: Item[] = [];
  let cursor = 0;
  const pushText = (end: number, last: boolean) => {
    const part = text.slice(cursor, end);
    cursor = end;
    if (!part.trim()) return;
    items.push({
      kind: "assistant",
      key: last && messageId ? `m-${messageId}` : `${base}-t${items.length}`,
      text: part,
      streaming: running && last,
      status: last ? status : "complete",
      messageId: last ? messageId : undefined,
    });
  };
  [...tools]
    .sort((a, b) => a.at - b.at)
    .forEach((tool, index) => {
      pushText(Math.min(Math.max(tool.at, cursor), text.length), false);
      const finished = tool.is_error !== null || !running;
      items.push({
        kind: "tool",
        key: `tool-${tool.id || `${base}-${index}`}`,
        toolUseId: tool.id,
        name: tool.name,
        summary: { kind: tool.kind, target: tool.target },
        result: finished ? { isError: tool.is_error ?? status !== "complete", text: tool.result ?? "" } : undefined,
      });
    });
  pushText(text.length, true);
  return items;
}

function permissionItem(body: PermissionBody): Item {
  return { kind: "permission", key: `perm-${body.request_id}`, body };
}

function applyEvent(state: ChatState, event: ServerEvent, errorText?: (code: string) => string): ChatState {
  switch (event.type) {
    case "conversation":
      return {
        ...state,
        id: event.conversation_id,
        title: event.title || state.title,
        model: isModel(event.model) ? event.model : state.model,
        agent: event.agent,
        project: event.project ?? state.project,
      };
    case "turn_start": {
      const items = [...state.items];
      for (let i = items.length - 1; i >= 0; i -= 1) {
        const item = items[i]!;
        if (item.kind === "user") {
          if (!item.messageId) items[i] = { ...item, messageId: event.message_id };
          break;
        }
      }
      return { ...state, items, running: true };
    }
    case "delta": {
      const last = state.items[state.items.length - 1];
      if (last && last.kind === "assistant" && last.streaming) {
        return { ...state, running: true, items: [...state.items.slice(0, -1), { ...last, text: last.text + event.text }] };
      }
      const item: Item = { kind: "assistant", key: nextKey("a"), text: event.text, streaming: true, status: "complete" };
      return { ...state, running: true, items: [...state.items, item] };
    }
    case "tool_use": {
      if (state.items.some((item) => item.kind === "tool" && item.toolUseId === event.tool_use_id)) return state;
      const item: Item = { kind: "tool", key: `tool-${event.tool_use_id}`, toolUseId: event.tool_use_id, name: event.name, summary: event.summary };
      return { ...state, items: [...closeStreaming(state.items), item] };
    }
    case "tool_result":
      return {
        ...state,
        items: state.items.map((item) =>
          item.kind === "tool" && item.toolUseId === event.tool_use_id ? { ...item, result: { isError: event.is_error, text: event.text } } : item,
        ),
      };
    case "permission_request": {
      if (state.items.some((item) => item.kind === "permission" && item.body.request_id === event.request_id)) return state;
      return { ...state, items: [...closeStreaming(state.items), permissionItem(event)] };
    }
    case "permission_resolved":
      return {
        ...state,
        items: state.items.map((item) =>
          item.kind === "permission" && item.body.request_id === event.request_id
            ? { ...item, sending: false, resolution: { decision: event.decision, by: event.by } }
            : item,
        ),
      };
    case "done": {
      const status = event.stopped ? "stopped" : "complete";
      const items = closeStreaming(state.items, status);
      if (event.message_id) {
        for (let i = items.length - 1; i >= 0; i -= 1) {
          const item = items[i]!;
          if (item.kind === "assistant") {
            items[i] = { ...item, messageId: event.message_id };
            break;
          }
          if (item.kind === "user") break;
        }
      }
      return { ...state, items, running: false, stopping: false };
    }
    case "error": {
      if (event.code === "unknown_conversation" && state.loading) {
        return { ...state, loading: false, loadError: errorText ? errorText(event.code) : event.code };
      }
      const text = errorText ? errorText(event.code) : event.code;
      const notice: Item = { kind: "notice", key: nextKey("n"), tone: "bad", text };
      if (event.code === "busy" || event.code === "unknown_request") return { ...state, items: [...state.items, notice] };
      return { ...state, items: [...closeStreaming(state.items, "error"), notice], running: false, stopping: false };
    }
    case "history": {
      const items: Item[] = [];
      for (const message of event.messages) {
        if (message.role === "user") {
          items.push({ kind: "user", key: `m-${message.id}`, messageId: message.id, text: message.text, attachments: (message.attachments ?? []).map(chipOf) });
        } else if (message.role === "assistant") {
          items.push(...answerItems(`m-${message.id}`, message.text, message.tools ?? [], message.status, false, message.id));
        }
      }
      return { ...state, items, loading: false, loadError: null };
    }
    case "state": {
      const items = [...state.items];
      // A reconnect in the middle of a turn: the answer so far comes from the server, not from the old socket.
      if (event.state === "running" && event.partial) {
        items.push(...answerItems(`part-${event.partial.turn_id}`, event.partial.text, event.partial.tools, "complete", true));
      }
      for (const body of event.pending_permissions) {
        if (!items.some((item) => item.kind === "permission" && item.body.request_id === body.request_id)) items.push(permissionItem(body));
      }
      return { ...state, items, running: event.state === "running" };
    }
    default:
      return state;
  }
}

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "reset":
      return initialState(action.id);
    case "local_send": {
      const item: Item = { kind: "user", key: nextKey("u"), text: action.text, attachments: action.attachments };
      return { ...state, items: [...state.items, item], running: true };
    }
    case "local_stop":
      return { ...state, stopping: true };
    case "local_model":
      return { ...state, model: action.model };
    case "local_project":
      return { ...state, project: action.project };
    case "permission_sending":
      return {
        ...state,
        items: state.items.map((item) => (item.kind === "permission" && item.body.request_id === action.requestId ? { ...item, sending: true } : item)),
      };
    case "notice":
      return { ...state, items: [...state.items, { kind: "notice", key: nextKey("n"), tone: action.tone, text: action.text }] };
    case "event":
      return applyEvent(state, action.event, action.errorText);
  }
}
