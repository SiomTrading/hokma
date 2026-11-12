import { getLocale, t } from "@/i18n";
import { RESTART_REASON, SHUTDOWN_REASON, UPDATING_REASON, enterOff, isOff, watchEngine } from "./restart";
import { clearOwnerOnce, getOwnerNonce, getOwnerOnce } from "./owner";
import { deleteDeviceKey } from "./devicekey";
import { forgetSession, onEngineReconnected } from "./session";
import { clearToken, getToken } from "./token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Stable machine reason the backend sends next to `code` on some errors (link downloads); empty when absent. */
  readonly reason: string;
  /** Extra fields the backend sent with the error (for example the counts of a folder that is not empty). */
  readonly data: Record<string, unknown>;

  constructor(status: number, code: string, message: string, reason = "", data: Record<string, unknown> = {}) {
    super(message);
    this.data = data;
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.reason = reason;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** 0 disables the timeout (streams, long downloads). Default 30000. */
  timeoutMs?: number;
  /** Internal: the call already retried once after registering the owner confirmation. */
  ownerRetried?: boolean;
}

const DEFAULT_TIMEOUT_MS = 30_000;

function isBodyInit(body: unknown): body is BodyInit {
  return (
    typeof body === "string" ||
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    body instanceof URLSearchParams
  );
}

/** Extracts a user-safe message from a backend error body. Never exposes stack traces. */
async function readError(res: Response): Promise<ApiError> {
  let code = "http_error";
  let reason = "";
  let extra: Record<string, unknown> = {};
  let message = t("api.error");
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object") {
      const body = data as Record<string, unknown>;
      const detail = body.detail ?? body;
      extra = detail && typeof detail === "object" && !Array.isArray(detail) ? (detail as Record<string, unknown>) : body;
      if (typeof detail === "string") {
        message = detail;
      } else if (Array.isArray(detail) && detail.length > 0) {
        const first = detail[0] as Record<string, unknown>;
        if (typeof first?.msg === "string") message = first.msg;
        code = "validation_error";
      } else if (detail && typeof detail === "object") {
        const d = detail as Record<string, unknown>;
        if (typeof d.message === "string") message = d.message;
        if (typeof d.code === "string") code = d.code;
        else if (typeof d.reason === "string") code = d.reason;
        if (typeof d.reason === "string") reason = d.reason;
      }
      if (typeof body.code === "string") code = body.code;
      else if (typeof body.reason === "string") code = body.reason;
      if (typeof body.reason === "string") reason = body.reason;
    }
  } catch {
    /* body is not JSON: keep the generic message */
  }
  return new ApiError(res.status, code, message, reason, extra);
}

export async function request(path: string, options: RequestOptions = {}): Promise<Response> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Accept-Language": getLocale(),
    ...(token ? { "X-Hokma-Token": token, "X-Hokma-Owner": getOwnerNonce() } : {}),
    ...options.headers,
  };
  let body: BodyInit | undefined;
  if (options.body !== undefined) {
    if (isBodyInit(options.body)) {
      body = options.body;
    } else {
      body = JSON.stringify(options.body);
      headers["Content-Type"] = "application/json";
    }
  }

  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  options.signal?.addEventListener("abort", () => controller.abort(), { once: true });

  let res: Response;
  try {
    res = await fetch(path, { method: options.method ?? "GET", headers, body, signal: controller.signal });
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw new ApiError(0, "network", t("api.network"));
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401) {
    clearToken();
    throw new ApiError(401, "unauthorized", t("api.unauthorized"));
  }
  if (!res.ok) {
    const error = await readError(res);
    if (error.code === "owner_confirmation_required" && !options.ownerRetried && (await registerOwner())) {
      return request(path, { ...options, ownerRetried: true });
    }
    throw error;
  }
  return res;
}

let ownerRegistered = false;

/**
 * Tells the engine which confirmation this tab holds. The first registration of each start of Hokma needs the
 * one-time code of the start link; afterwards the engine accepts the same confirmation again, so calling it on
 * every load and after a refusal is safe.
 */
export async function registerOwner(): Promise<boolean> {
  if (!getToken()) return false;
  try {
    const res = await fetch("/api/session/owner-nonce", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Hokma-Token": getToken() ?? "" },
      body: JSON.stringify({ nonce: getOwnerNonce(), once: getOwnerOnce() ?? "" }),
    });
    ownerRegistered = res.ok;
    if (res.ok) clearOwnerOnce();                    // one use: the engine has consumed it
  } catch {
    ownerRegistered = false;
  }
  return ownerRegistered;
}

export function ownerIsRegistered(): boolean {
  return ownerRegistered;
}

/**
 * Settings > General > close the session of this browser. The engine forgets the public key and expires the cookie;
 * the page deletes the device key from IndexedDB, forgets its token and goes back to the entry screen. An owner
 * call: it carries the token and the confirmation like any other.
 */
export async function logoutSession(): Promise<void> {
  await request("/api/session/logout", { method: "POST" });
  await deleteDeviceKey();
  forgetSession();
  ownerRegistered = false;
  clearToken();
}

async function json<T>(path: string, options: RequestOptions): Promise<T> {
  const res = await request(path, options);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  request,
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) => json<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    json<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    json<T>(path, { ...options, method: "PUT", body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    json<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) => json<T>(path, { ...options, method: "DELETE" }),
};

/** Close code the backend uses when the handshake token is wrong. */
export const WS_UNAUTHORIZED = 4401;

export interface SocketHandlers {
  onOpen?: () => void;
  onMessage?: (data: unknown) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: () => void;
  /** Reconnect with backoff (0.5 s to 10 s). Default true. */
  reconnect?: boolean;
}

export interface Socket {
  send: (data: unknown) => boolean;
  close: () => void;
}

export function socketUrl(path: string, token: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const sep = path.includes("?") ? "&" : "?";
  return `${scheme}://${window.location.host}${path}${sep}token=${encodeURIComponent(token)}`;
}

/** Opens an authenticated WebSocket. The token travels in the handshake URL. */
export function openSocket(path: string, handlers: SocketHandlers = {}): Socket {
  let ws: WebSocket | null = null;
  let closedByUser = false;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const connect = (): void => {
    const token = getToken();
    if (!token || isOff()) return;
    ws = new WebSocket(socketUrl(path, token));
    ws.onopen = () => {
      if (attempt > 0) onEngineReconnected();          // the engine may have restarted: give it the owner confirmation back
      attempt = 0;
      handlers.onOpen?.();
    };
    ws.onmessage = (event: MessageEvent) => {
      if (typeof event.data !== "string") {
        handlers.onMessage?.(event.data);
        return;
      }
      try {
        handlers.onMessage?.(JSON.parse(event.data));
      } catch {
        handlers.onMessage?.(event.data);
      }
    };
    ws.onerror = () => handlers.onError?.();
    ws.onclose = (event: CloseEvent) => {
      handlers.onClose?.(event);
      if (event.reason === SHUTDOWN_REASON) {
        closedByUser = true;                           // shut down on purpose: no reconnect, no crash watch
        enterOff();
        return;
      }
      if (isOff()) return;
      if (event.reason === UPDATING_REASON) watchEngine({ mode: "update" });
      else if (event.reason === RESTART_REASON) watchEngine({ mode: "restart" });
      else if (event.code === 1006 && !closedByUser) watchEngine({ mode: "crash" });
      if (event.code === WS_UNAUTHORIZED) {
        clearToken();
        return;
      }
      if (closedByUser || handlers.reconnect === false) return;
      const delay = Math.min(10_000, 500 * 2 ** attempt);
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    };
  };

  connect();

  return {
    send: (data) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(typeof data === "string" ? data : JSON.stringify(data));
      return true;
    },
    close: () => {
      closedByUser = true;
      if (retryTimer) clearTimeout(retryTimer);
      ws?.close();
    },
  };
}
