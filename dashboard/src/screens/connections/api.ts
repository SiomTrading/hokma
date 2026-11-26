import { api, ApiError } from "@/api";
import { t } from "@/i18n";
import type { AddAnswer, ConnectionCard, ConnectionsList, DiagnosticAnswer, FinishAnswer, SampleAnswer, SampleProvider, SchwabAccountPanel, TestResult, UseKey } from "./types";

const enc = encodeURIComponent;
const base = (id: string) => "/api/connections/" + enc(id);
const SCHWAB = "/api/connections/schwab";

/** The secret fields of a key: `key`, `key_id` + `secret` (Alpaca) or `client_id` + `client_secret` (Schwab). */
export interface KeyPayload {
  key?: string;
  key_id?: string;
  secret?: string;
  client_id?: string;
  client_secret?: string;
}

/** The only place of the panel that talks to the keys and the provider switches. */
export const connectionsApi = {
  list: () => api.get<ConnectionsList>("/api/connections"),
  saveKey: (id: string, body: KeyPayload) => api.put<{ id: string; key_state: string }>(base(id) + "/key", body),
  /** Saves the key, switches the provider on and tests it. A failed test drops the new key (the old one stays). */
  add: (id: string, body: KeyPayload) => api.post<AddAnswer>(base(id) + "/add", body, { timeoutMs: 30_000 }),
  /** The broker that gives the live data when two are connected (SPEC-016 3.6). */
  setDataSource: (provider: string) => api.put<{ data_source: string }>("/api/connections/data-source", { provider }),
  remove: (id: string) => api.post<ConnectionCard>(base(id) + "/remove", {}),
  setEnabled: (id: string, enabled: boolean) => api.put<ConnectionCard>(base(id) + "/enabled", { enabled }),
  /** `asset_class` is the name of the field for every use (`bars`, `options` and `news` too). `order` has every provider of the use. */
  setPriority: (use: UseKey, order: string[]) =>
    api.put<{ groups?: ConnectionsList["groups"]; uses?: ConnectionsList["uses"] }>("/api/connections/priority", { asset_class: use, order }),
  test: (id: string) => api.post<TestResult>(base(id) + "/test", {}, { timeoutMs: 20_000 }),
};

/** The sign-in of Schwab (5.3) and its account panel (5.7). The address that comes back is pasted by the student. */
export const schwabApi = {
  loginStart: () => api.post<{ auth_url: string }>(SCHWAB + "/login/start", {}),
  /** The desktop window cannot open a browser tab by itself: the engine opens the system browser. */
  loginOpen: (url: string) => api.post<{ opened: boolean }>(SCHWAB + "/login/open", { url }),
  loginFinish: (redirectUrl: string) => api.post<FinishAnswer>(SCHWAB + "/login/finish", { redirect_url: redirectUrl }, { timeoutMs: 30_000 }),
  account: () => api.get<SchwabAccountPanel>(SCHWAB + "/account"),
  /** One measuring run of the data Schwab sends (reads only, no orders): it listens for about `seconds` seconds and saves a file. */
  diagnostic: (seconds: number) =>
    api.post<DiagnosticAnswer>("/api/schwab/diagnostic/streaming", { symbol: "QQQ", seconds }, { timeoutMs: (seconds + 90) * 1000 }),
};

/** The anonymous sample of the account: one read of what Schwab or Alpaca answers for the accounts, saved in the downloads folder with every id as XXXX. */
export const sampleApi = {
  save: (provider: SampleProvider = "schwab") =>
    api.post<SampleAnswer>(provider === "alpaca" ? "/api/alpaca/account-sample" : "/api/schwab/diagnostic/account-sample", {}, { timeoutMs: 30_000 }),
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

/** True inside the desktop app, whose window cannot open a browser tab by itself. */
export function inDesktopApp(): boolean {
  return typeof window !== "undefined" && ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);
}

export interface MailState {
  connected: boolean;
  email: string | null;
  store_available: boolean;
  apppasswords_url: string;
}

/** Gmail of the student (app password). The password goes out once and never comes back. */
export const mailApi = {
  state: () => api.get<MailState>("/api/mail"),
  connect: (email: string, password: string) => api.put<{ connected: true; email: string }>("/api/mail", { email, password }, { timeoutMs: 45_000 }),
  disconnect: () => api.delete<{ connected: false }>("/api/mail"),
  notify: () => api.get<MailNotify>("/api/mail/notify"),
  saveNotify: (fallback: boolean, jobs: number[]) => api.put<MailNotify>("/api/mail/notify", { fallback, jobs }),
  sendTest: () => api.post<{ sent: true }>("/api/mail/notify/test", {}, { timeoutMs: 45_000 }),
};

export interface MailNotify {
  connected: boolean;
  email: string | null;
  fallback: boolean;
  jobs: Array<{ id: number; name: string; mail: boolean }>;
}
