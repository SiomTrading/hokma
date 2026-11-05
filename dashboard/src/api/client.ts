import { getLocale, t } from "@/i18n";
import { clearToken, getToken } from "./token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** 0 disables the timeout (streams, long downloads). Default 30000. */
  timeoutMs?: number;
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
  let message = t("api.error");
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object") {
      const body = data as Record<string, unknown>;
      const detail = body.detail ?? body;
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
      }
      if (typeof body.code === "string") code = body.code;
    }
  } catch {
    /* body is not JSON: keep the generic message */
  }
  return new ApiError(res.status, code, message);
}

export async function request(path: string, options: RequestOptions = {}): Promise<Response> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Accept-Language": getLocale(),
    ...(token ? { "X-Hokma-Token": token } : {}),
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
  if (!res.ok) throw await readError(res);
  return res;
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

function socketUrl(path: string, token: string): string {
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
    if (!token) return;
    ws = new WebSocket(socketUrl(path, token));
    ws.onopen = () => {
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
