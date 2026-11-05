import { vi } from "vitest";
import { setToken } from "@/api";

export interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Handler = (call: Call) => unknown;

/** Replaces `fetch` with routes keyed by "METHOD /path" (query string ignored). Returns the list of calls. */
export function mockApi(routes: Record<string, Handler | unknown>): Call[] {
  setToken("test-session-token-0123456789");
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const method = (init?.method ?? "GET").toUpperCase();
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      const call = { method, path: url.pathname + url.search, body };
      calls.push(call);
      const route = routes[`${method} ${url.pathname}`];
      if (route === undefined) return new Response(JSON.stringify({ detail: "not mocked" }), { status: 404 });
      const out = typeof route === "function" ? (route as Handler)(call) : route;
      if (out instanceof Response) return out;
      return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

export function failure(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } });
}
