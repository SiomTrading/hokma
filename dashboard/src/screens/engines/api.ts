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
}

export interface PingResult {
  ok: boolean;
  status: string;
  code: string;
}

export interface DeviceLogin {
  state: "idle" | "waiting" | "done" | "failed" | "no_cli";
  url: string;
  code: string;
}

export const enginesApi = {
  list: () => api.get<EnginesListing>("/api/engines"),
  addApi: (body: { provider: string; key?: string; base_url?: string; model?: string; label?: string }) =>
    api.post<Connection>("/api/engines/api", body),
  addCli: (body: { engine: string; auth_kind: string; label: string; key?: string }) => api.post<Connection>("/api/engines/cli", body),
  test: (id: number) => api.post<PingResult>(`/api/engines/${id}/test`, undefined, { timeoutMs: 60000 }),
  makeDefault: (id: number) => api.put<{ ok: boolean }>(`/api/engines/${id}/default`),
  remove: (id: number) => api.delete<{ ok: boolean }>(`/api/engines/${id}`),
  loginStart: () => api.post<DeviceLogin>("/api/engines/codex/login", undefined, { timeoutMs: 30000 }),
  loginStatus: () => api.get<DeviceLogin>("/api/engines/codex/login"),
  loginCancel: () => api.delete<{ ok: boolean }>("/api/engines/codex/login"),
};

/** The failure codes of the ping, mapped to a text key. Unknown codes fall back to the generic engine error. */
export function errorKey(code: string): string {
  return ["region", "auth", "quota", "network", "not_found"].includes(code) ? `eng.err.${code}` : "eng.err.engine_error";
}
