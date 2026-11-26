import { DEFAULT_MODEL, isModel } from "./types";
import { attachmentUrl } from "./chatApi";
import type { GuardBlock, ModelId, PermissionBody, ProjectRef, ServerEvent, StoredAttachment, StoredTool, TurnOrigin } from "./types";

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
  /** Why the upload failed, as a catalog reason (empty_file, too_large); absent when the server gave none we know. */
  reason?: "empty_file" | "too_large";
  /** A voice note whose text was transcribed into the message. */
  transcribed?: boolean;
}

export type Item =
  | { kind: "user"; key: string; messageId?: string; text: string; attachments: AttachmentChip[]; /** Saved, but waiting for the lead to finish the summary of its agents (bug 1257). */ waiting?: boolean; /** Never sent: Hokma closed while it waited. */ unsent?: boolean }
  | { kind: "assistant"; key: string; text: string; streaming: boolean; status: "complete" | "stopped" | "error"; messageId?: string; afterTool?: boolean; origin?: TurnOrigin }
  | { kind: "permission"; key: string; body: PermissionBody; sending?: boolean; resolution?: { decision: "allow" | "deny"; by: string } }
  | { kind: "block"; key: string; block: GuardBlock }
  | { kind: "notice"; key: string; tone: "info" | "warn" | "bad"; text: string; action?: "open_engines" };

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
  /** Who opened the turn that runs, when it was not the owner (bug 1257): its text is a new message of the assistant that carries this mark. */
  origin?: TurnOrigin;
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
/** Error codes whose own text says it all; any other code shows the message from the engine under the translated text (bug 1095). */
const SELF_EXPLAINED = new Set(["bad_json", "bad_message", "unknown_conversation", "busy", "unknown_model", "unknown_agent",
  "unknown_project", "unknown_request", "attachment_not_found", "too_many_sessions", "no_engine",
  // Bug 1174: a provider's refusal already has its own translated text; its raw answer goes only to engine.log.
  "auth", "region", "quota", "network", "model", "unavailable", "claude_disabled", "workspace", "device_auth", "model_choice"]);

/** Bug 1174: the engine's own words help ("command not found: git"); a provider's raw HTTP answer or JSON does not:
 * it is English, one long line, and engine.log already has it. */
export function readableDetail(message: unknown): string {
  if (typeof message !== "string") return "";
  const text = message.trim();
  if (!text || /^HTTP \d{3}\b/.test(text) || /^[[{]/.test(text) || /[[{]\s*"(error|code|message)"/.test(text)) return "";
  return text.slice(0, 600);
}

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

/** Text that follows a tool call must not stick to the text before it: "...the file.Done" becomes two paragraphs. */
function needsBreak(before: string, after: string): boolean {
  return before.length > 0 && after.length > 0 && !/\s$/.test(before) && !/^\s/.test(after);
}

/**
 * The text of an answer with a paragraph break where a tool was called, when the server left none there. `at` is how many
 * characters of the answer came before the tool call. The tools are not in the chat (they are in the side panel), but the
 * text on both sides of one must still read as separate paragraphs, as a live answer does.
 */
export function joinAroundTools(text: string, tools: StoredTool[]): string {
  const cuts = [...new Set(tools.map((tool) => Math.min(Math.max(tool.at, 0), text.length)))].sort((a, b) => a - b);
  let out = "";
  let cursor = 0;
  for (const cut of cuts) {
    out += text.slice(cursor, cut);
    cursor = cut;
    if (cut < text.length && needsBreak(out, text.slice(cut, cut + 1))) out += "\n\n";
  }
  return out + text.slice(cursor);
}

/**
 * Items of one answer: its whole text. The tools and the agents the answer used are not in the chat, they are in the side
 * panel (SPEC-013), so a saved answer and a live one read the same. `running` is a turn still in progress.
 */
function answerItems(base: string, text: string, tools: StoredTool[], status: "complete" | "stopped" | "error", running: boolean, messageId?: string, origin?: TurnOrigin): Item[] {
  const whole = joinAroundTools(text, tools);
  if (!whole.trim()) return [];
  // A reconnect right after a tool call: the next piece of text still has to start a new paragraph.
  const afterTool = running && tools.some((tool) => tool.at >= text.length);
  return [{ kind: "assistant", key: messageId ? `m-${messageId}` : `${base}-t0`, text: whole, streaming: running, status, messageId, ...(afterTool ? { afterTool } : {}), ...(origin ? { origin } : {}) }];
}

function blockItem(block: GuardBlock): Item {
  return { kind: "block", key: `block-${block.id}`, block: { id: block.id, rule: block.rule, tool: block.tool, target: block.target, at: block.at, ...(block.reason ? { reason: block.reason } : {}) } };
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
        // null is a real answer now: a conversation without a project is General
        project: event.project ?? null,
      };
    case "engine_notice":
      return { ...state, items: [...state.items, { kind: "notice", key: nextKey("n"), tone: "warn", text: event.message }] };
    case "turn_start": {
      // A turn nobody asked for (the summary of the background agents) has no message of the owner to mark.
      if (event.origin) return { ...state, running: true, origin: event.origin };
      const items = [...state.items];
      for (let i = items.length - 1; i >= 0; i -= 1) {
        const item = items[i]!;
        if (item.kind === "user") {
          if (!item.messageId) items[i] = { ...item, messageId: event.message_id ?? undefined };
          break;
        }
      }
      // His turn starts: the waiting state ends on that same message, which is not added again (bug 1257).
      return { ...state, items: items.map((item) => (item.kind === "user" && item.waiting && item.messageId === event.message_id ? { ...item, waiting: false } : item)), running: true, origin: undefined };
    }
    case "queued": {
      // The message is already on screen (the local send): it only gets its saved id and the waiting state. One that is
      // not (another tab, a reload in the middle) appears now, so it never looks lost.
      const items = [...state.items];
      let at = items.findIndex((item) => item.kind === "user" && item.messageId === event.message_id);
      if (at < 0) {
        for (let i = items.length - 1; i >= 0; i -= 1) {
          const item = items[i]!;
          if (item.kind === "user" && !item.messageId && item.text === event.text) {
            at = i;
            break;
          }
        }
      }
      if (at >= 0) {
        const item = items[at] as Extract<Item, { kind: "user" }>;
        items[at] = { ...item, messageId: event.message_id, waiting: true };
      } else {
        items.push({ kind: "user", key: `m-${event.message_id}`, messageId: event.message_id, text: event.text, attachments: [], waiting: true });
      }
      return { ...state, items, running: true };
    }
    case "queued_dropped":
      return { ...state, items: state.items.map((item) => (item.kind === "user" && item.messageId === event.message_id ? { ...item, waiting: false, unsent: true } : item)) };
    case "delta": {
      const last = state.items[state.items.length - 1];
      if (last && last.kind === "assistant" && last.streaming) {
        const gap = last.afterTool && needsBreak(last.text, event.text) ? "\n\n" : "";
        return { ...state, running: true, items: [...state.items.slice(0, -1), { ...last, text: last.text + gap + event.text, afterTool: false }] };
      }
      const item: Item = { kind: "assistant", key: nextKey("a"), text: event.text, streaming: true, status: "complete", ...(state.origin ? { origin: state.origin } : {}) };
      return { ...state, running: true, items: [...state.items, item] };
    }
    case "tool_use": {
      // The tool is in the side panel; here it only marks that the text that follows starts a new paragraph.
      const last = state.items[state.items.length - 1];
      if (!last || last.kind !== "assistant" || !last.streaming || last.afterTool) return state;
      return { ...state, items: [...state.items.slice(0, -1), { ...last, afterTool: true }] };
    }
    case "guard_block": {
      if (state.items.some((item) => item.kind === "block" && item.block.id === event.id)) return state;
      return { ...state, items: [...closeStreaming(state.items), blockItem(event)] };
    }
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
      return { ...state, items, running: false, stopping: false, origin: undefined };
    }
    case "error": {
      if (event.code === "unknown_conversation" && state.loading) {
        return { ...state, loading: false, loadError: errorText ? errorText(event.code) : event.code };
      }
      const base = errorText ? errorText(event.code) : event.code;
      // The engine's own words after the short text (task 3171): "something failed" alone told nobody what to fix.
      const detail = !SELF_EXPLAINED.has(event.code) ? readableDetail(event.message) : "";
      const text = detail ? `${base}\n${detail}` : base;
      const notice: Item = { kind: "notice", key: nextKey("n"), tone: "bad", text, ...(event.code === "no_engine" || event.code === "auth" || event.code === "model_choice" ? { action: "open_engines" as const } : {}) };
      if (event.code === "busy" || event.code === "unknown_request") return { ...state, items: [...state.items, notice] };
      return { ...state, items: [...closeStreaming(state.items, "error"), notice], running: false, stopping: false, origin: undefined };
    }
    case "history": {
      const items: Item[] = [];
      for (const message of event.messages) {
        if (message.role === "user") {
          items.push({ kind: "user", key: `m-${message.id}`, messageId: message.id, text: message.text, attachments: (message.attachments ?? []).map(chipOf), ...(message.unsent ? { unsent: true } : {}) });
        } else if (message.role === "assistant") {
          items.push(...answerItems(`m-${message.id}`, message.text, message.tools ?? [], message.status, false, message.id, message.origin));
          for (const block of message.blocks ?? []) items.push(blockItem(block));
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
