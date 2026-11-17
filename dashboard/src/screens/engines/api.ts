import { api } from "@/api";

export interface Connection {
  id: number;
  engine: string;
  label: string;
  auth_kind: string;
  provider: string | null;
  host: string | null;
  status: string;
  status_detail: string | null;
  is_default: boolean;
  /** Levels to models: picked from the provider's list ("auto" mark) or chosen by the student (bug 1173). */
  tier_models?: Record<string, string>;
}

export interface Provider {
  id: string;
  label: string;
  wire: string;
  base_url: string;
  local: boolean;
}

export interface EnginesListing {
  connections: Connection[];
  detect: { cli: Record<string, boolean>; codex_login: boolean; claude_import: boolean; providers: Provider[] };
  store_available: boolean;
  /** The model button: one group per engine that is connected and can run (catalog models). */
  models?: ModelGroup[];
  /** The engine that will answer, by the rule the chat uses (bug 1095). */
  active?: ActiveEngine;
  /** The student pressed Disconnect on the Claude card; kept by the server (a removed Claude row), so it survives leaving Settings and restarts. */
  claude_disabled_by_user?: boolean;
}

export interface ModelGroup {
  connection_id: number | null;
  engine: string;
  label: string;
  models: { id: "haiku" | "sonnet" | "opus"; tier: string; name: string }[];
}

export interface ActiveEngine {
  id: number | null;
  engine: string;
  label: string;
  replaced: boolean;
  available: boolean;
  saved: string | null;
}

export interface PingResult {
  ok: boolean;
  status: string;
  code: string;
  /** The model the test used (bug 1173: named when the provider refuses it). */
  model?: string;
}

export interface DeviceLogin {
  state: "idle" | "waiting" | "done" | "failed" | "no_cli";
  url: string;
  code: string;
  /** Why a sign-in failed (bug 1175): workspace, device_auth, auth... Empty when unknown. */
  reason?: string;
}

export const enginesApi = {
  list: (conversationId?: string | null) =>
    api.get<EnginesListing>(conversationId ? "/api/engines?conversation_id=" + encodeURIComponent(conversationId) : "/api/engines"),
  addApi: (body: { provider: string; key?: string; base_url?: string; model?: string; label?: string }) =>
    api.post<Connection>("/api/engines/api", body),
  addCli: (body: { engine: string; auth_kind: string; label: string; key?: string }) => api.post<Connection>("/api/engines/cli", body),
  test: (id: number) => api.post<PingResult>(`/api/engines/${id}/test`, undefined, { timeoutMs: 60000 }),
  claudeConnection: () => api.post<Connection>("/api/engines/claude"),
  switchEngine: (conversationId: string, connectionId: number) =>
    api.put<{ ok: boolean; notice: string }>("/api/conversations/" + encodeURIComponent(conversationId) + "/engine", { connection_id: connectionId }),
  makeDefault: (id: number) => api.put<{ ok: boolean }>(`/api/engines/${id}/default`),
  remove: (id: number) => api.delete<{ ok: boolean }>(`/api/engines/${id}`),
  loginStart: () => api.post<DeviceLogin>("/api/engines/codex/login", undefined, { timeoutMs: 30000 }),
  loginStatus: () => api.get<DeviceLogin>("/api/engines/codex/login"),
  loginCancel: () => api.delete<{ ok: boolean }>("/api/engines/codex/login"),
  /** The provider's chat models for this key, newest first (read again; the saved list without network). */
  models: (id: number) => api.get<{ models: string[] }>(`/api/engines/${id}/models`, { timeoutMs: 30000 }),
  setTiers: (id: number, tiers: Record<string, string>) => api.put<Connection>(`/api/engines/${id}/tiers`, { tiers }),
};

/** The failure codes of the ping, mapped to a text key. Unknown codes fall back to the generic engine error. */
export function errorKey(code: string): string {
  // Adapter codes carry the CLI name (claude_not_found, gemini_not_found): all of them read "I cannot find it" (1158).
  if (code.endsWith("_not_found")) return "eng.err.not_found";
  return ["region", "auth", "model", "model_choice", "quota", "unavailable", "network", "not_found", "workspace", "device_auth"].includes(code)
    ? `eng.err.${code}`
    : "eng.err.engine_error";
}
