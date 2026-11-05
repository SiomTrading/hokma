import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, clearToken, getToken, initToken, openSocket } from "@/api";
import { setToken } from "./token";
import { t } from "@/i18n";

const TOKEN = "abcDEF1234567890_-xyz";

describe("session token", () => {
  beforeEach(() => {
    clearToken();
    window.history.replaceState(null, "", "/");
  });

  it("reads #t=, stores it and removes it from the address bar", () => {
    window.history.replaceState(null, "", `/app?x=1#t=${TOKEN}`);
    initToken();
    expect(getToken()).toBe(TOKEN);
    expect(localStorage.getItem("hokma.token")).toBe(TOKEN);
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/app?x=1");
  });

  it("ignores a malformed token but still cleans the bar", () => {
    window.history.replaceState(null, "", "/#t=bad token<script>");
    initToken();
    expect(getToken()).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("keeps the stored token when the URL has none", () => {
    setToken(TOKEN);
    initToken();
    expect(getToken()).toBe(TOKEN);
  });
});

describe("rest client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    clearToken();
    setToken(TOKEN);
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sends X-Hokma-Token and the locale, and parses JSON", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const out = await api.get<{ ok: boolean }>("/api/x");
    expect(out).toEqual({ ok: true });
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/api/x");
    expect(init.headers["X-Hokma-Token"]).toBe(TOKEN);
    expect(init.headers["Accept-Language"]).toBe("es");
  });

  it("serializes JSON bodies and does not force a content type on FormData", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    await api.post("/api/x", { a: 1 });
    expect(fetchMock.mock.calls[0]![1].body).toBe('{"a":1}');
    expect(fetchMock.mock.calls[0]![1].headers["Content-Type"]).toBe("application/json");
    await api.post("/api/x", new FormData());
    expect(fetchMock.mock.calls[1]![1].headers["Content-Type"]).toBeUndefined();
  });

  it("maps backend errors to ApiError without leaking internals", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: { code: "limit", message: "Too many" } }), { status: 429 }));
    await expect(api.get("/api/x")).rejects.toMatchObject({ status: 429, code: "limit", message: "Too many" });
    fetchMock.mockResolvedValue(new Response("<html>trace</html>", { status: 500 }));
    const err = await api.get("/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe(t("api.error"));
  });

  it("clears the token on 401", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 401 }));
    await expect(api.get("/api/x")).rejects.toMatchObject({ status: 401, code: "unauthorized" });
    expect(getToken()).toBeNull();
  });

  it("turns network failures into a readable ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("failed"));
    await expect(api.get("/api/x")).rejects.toMatchObject({ status: 0, code: "network" });
  });
});

describe("websocket client", () => {
  class FakeSocket {
    static instances: FakeSocket[] = [];
    static OPEN = 1;
    readyState = 1;
    sent: string[] = [];
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: unknown }) => void) | null = null;
    onclose: ((e: { code: number }) => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(public url: string) {
      FakeSocket.instances.push(this);
    }
    send(data: string) {
      this.sent.push(data);
    }
    close() {}
  }

  beforeEach(() => {
    FakeSocket.instances = [];
    clearToken();
    setToken(TOKEN);
    vi.stubGlobal("WebSocket", FakeSocket);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("puts the token in the handshake URL and parses JSON frames", () => {
    const onMessage = vi.fn();
    const sock = openSocket("/api/stream", { onMessage });
    const ws = FakeSocket.instances[0]!;
    expect(ws.url).toMatch(/^ws:\/\/.+\/api\/stream\?token=abcDEF1234567890_-xyz$/);
    ws.onmessage?.({ data: '{"a":1}' });
    expect(onMessage).toHaveBeenCalledWith({ a: 1 });
    expect(sock.send({ b: 2 })).toBe(true);
    expect(ws.sent).toEqual(['{"b":2}']);
  });

  it("appends the token with & when the path already has a query", () => {
    openSocket("/api/stream?id=3");
    expect(FakeSocket.instances[0]!.url).toContain("/api/stream?id=3&token=");
  });

  it("drops the token when the server closes with 4401 and does not reconnect", () => {
    vi.useFakeTimers();
    openSocket("/api/stream");
    FakeSocket.instances[0]!.onclose?.({ code: 4401 });
    expect(getToken()).toBeNull();
    vi.advanceTimersByTime(20_000);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.useRealTimers();
  });

  it("reconnects with backoff after an unexpected close, and stops after close()", () => {
    vi.useFakeTimers();
    const sock = openSocket("/api/stream");
    FakeSocket.instances[0]!.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(500);
    expect(FakeSocket.instances).toHaveLength(2);
    sock.close();
    FakeSocket.instances[1]!.onclose?.({ code: 1000 });
    vi.advanceTimersByTime(20_000);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.useRealTimers();
  });
});
