import { api, getToken } from "@/api";
import { t } from "@/i18n";
import type { ChatOptions, ConversationSummary, UploadedAttachment } from "./types";

export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
export const MAX_ATTACHMENTS = 10;
/** From this share of the weekly cap the bar turns orange and the chat warns once. */
export const USAGE_WARN_PCT = 80;

/** `archived` lists only the archived conversations; without it they are left out. */
export async function listConversations(query: string, signal?: AbortSignal, archived = false): Promise<ConversationSummary[]> {
  const params = new URLSearchParams();
  if (query.trim()) params.set("q", query.trim());
  if (archived) params.set("archived", "true");
  const text = params.toString();
  const path = text ? `/api/conversations?${text}` : "/api/conversations";
  const data = await api.get<{ conversations: ConversationSummary[] }>(path, { signal });
  return data.conversations;
}

export function patchConversation(id: string, body: { pinned?: boolean; archived?: boolean; title?: string }): Promise<ConversationSummary> {
  return api.patch<ConversationSummary>(`/api/conversations/${encodeURIComponent(id)}`, body);
}

export function deleteConversation(id: string): Promise<void> {
  return api.delete<void>(`/api/conversations/${encodeURIComponent(id)}`);
}

/** Where the thumbnail of an image sent in the chat is served (the session token travels in the address). */
export function attachmentUrl(id: string): string {
  return `/api/attachments/${encodeURIComponent(id)}?token=${encodeURIComponent(getToken() ?? "")}`;
}

/** Share of the weekly cap used, 0 to 100; null when the owner has not set a cap. */
export async function weeklyUsagePct(): Promise<number | null> {
  const data = await api.get<{ pct: number | null }>("/api/usage/weekly");
  return typeof data.pct === "number" ? data.pct : null;
}

export function chatOptions(): Promise<ChatOptions> {
  return api.get<ChatOptions>("/api/chat/options");
}

export function uploadAttachment(file: File): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append("file", file, file.name || "file");
  return api.post<UploadedAttachment>("/api/attachments", form, { timeoutMs: 180_000 });
}

export function saveToProject(
  conversationId: string,
  target: { message_id: string } | { attachment_id: string },
): Promise<{ path: string; project: { id: string; name: string } }> {
  return api.post(`/api/conversations/${encodeURIComponent(conversationId)}/save`, target);
}

const ERROR_CODES = new Set([
  "bad_json", "bad_message", "unknown_conversation", "busy", "unknown_model", "unknown_agent", "unknown_project",
  "unknown_request", "attachment_not_found", "too_many_sessions", "claude_not_found", "integrity", "engine_exited", "engine_error",
]);

/** Translated text for an engine error code. */
export function errorText(code: string): string {
  return ERROR_CODES.has(code) ? t(`chat.err.${code}`) : t("chat.err.generic");
}

/** "2 min", "1 h", "yesterday", "3 d" for the list. */
export function relativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, Math.floor((now - then) / 60_000));
  if (minutes < 1) return t("chat.time.now");
  if (minutes < 60) return t("chat.time.min", { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("chat.time.hour", { n: hours });
  const days = Math.floor(hours / 24);
  return days === 1 ? t("chat.time.yesterday") : t("chat.time.days", { n: days });
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
