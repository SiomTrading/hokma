import { api, ApiError } from "@/api";
import { t } from "@/i18n";
import type { AssetClass, ConnectionCard, ConnectionsList, TestResult } from "./types";

const enc = encodeURIComponent;
const base = (id: string) => "/api/connections/" + enc(id);

export interface KeyPayload {
  key?: string;
  key_id?: string;
  secret?: string;
}

/** The only place of the panel that talks to the keys and the provider switches. */
export const connectionsApi = {
  list: () => api.get<ConnectionsList>("/api/connections"),
  saveKey: (id: string, body: KeyPayload) => api.put<{ id: string; key_state: string }>(base(id) + "/key", body),
  removeKey: (id: string) => api.delete<{ id: string; key_state: string }>(base(id) + "/key"),
  setEnabled: (id: string, enabled: boolean) => api.put<ConnectionCard>(base(id) + "/enabled", { enabled }),
  setPriority: (assetClass: AssetClass, order: string[]) =>
    api.put<{ groups: ConnectionsList["groups"] }>("/api/connections/priority", { asset_class: assetClass, order }),
  test: (id: string) => api.post<TestResult>(base(id) + "/test", {}, { timeoutMs: 20_000 }),
};

/** Catalog text of a `connections.*` or `market.*` code in the active language; the server text when the code is new. */
export function codeText(code: string | null | undefined, fallback = ""): string {
  if (!code) return fallback;
  const prefix = code.startsWith("market.") ? "market" : "connections";
  const key = prefix + ".err." + code.replace(/^(market|connections)\./, "");
  const text = t(key);
  return text === key ? fallback || t("connections.err.unreachable") : text;
}

export function connectionErrorText(cause: unknown): string {
  if (cause instanceof ApiError) return codeText(cause.code, cause.message);
  return t("connections.err.unreachable");
}

export function isStoreUnavailable(cause: unknown): boolean {
  return cause instanceof ApiError && cause.code === "connections.store_unavailable";
}
