import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, clearToken, getToken, initToken, openSocket } from "@/api";
import { clearOwnerOnce, getOwnerOnce, resetOwnerForTests } from "./owner";
import { reloadWithHandoff } from "./handoff";
import { setToken } from "./token";
import { t } from "@/i18n";

const TOKEN = "abcDEF1234567890_-xyz";
const ONCE = "oneTimeSecret_0123456789";

describe("session token", () => {
  beforeEach(() => {
    clearToken();
    window.history.replaceState(null, "", "/");
  });

  it("reads #t=, stores it and removes it from the address bar", () => {
    window.history.replaceState(null, "", `/app?x=1#t=${TOKEN}`);
    initToken();
    expect(getToken()).toBe(TOKEN);
    expect(sessionStorage.length).toBe(0);                                  // nothing at rest in the browser profile
    expect(localStorage.getItem("hokma.token")).toBeNull();
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/app?x=1");
  });

  it("ignores a malformed token but still cleans the bar", () => {
    window.history.replaceState(null, "", "/#t=bad token<script>");
    initToken();
    expect(getToken()).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("never writes the token to localStorage or to a cookie, and removes one an older version left", async () => {
    localStorage.setItem("hokma.token", TOKEN);
    vi.resetModules();
    const fresh = await import("./token");
    expect(localStorage.getItem("hokma.token")).toBeNull();
    fresh.setToken(TOKEN);
    expect(Object.keys(localStorage)).not.toContain("hokma.token");
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain(TOKEN);
    expect(document.cookie).not.toContain(TOKEN);
    expect(sessionStorage.length).toBe(0);
  });

  it("survives a reload Hokma makes through window.name, which is emptied as soon as the next page reads it (bug 1038)", async () => {
    setToken(TOKEN);
    reloadWithHandoff(() => undefined);
    expect(window.name).toContain(TOKEN);
    expect(JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage), document.cookie])).not.toContain(TOKEN);
    vi.resetModules();
    const fresh = await import("./token");                                   // the next page load
    expect(fresh.getToken()).toBe(TOKEN);
    expect(window.name).toBe("");
    window.dispatchEvent(new Event("pagehide"));                              // leaving by any other way writes nothing
    expect(window.name).toBe("");
    expect(sessionStorage.length).toBe(0);
    window.name = "";
  });

  it("loses the session when the tab was closed: nothing is left to read", async () => {
    setToken(TOKEN);
    window.name = "";                                                         // a closed tab takes its window.name with it
    vi.resetModules();
    const fresh = await import("./token");
    expect(fresh.getToken()).toBeNull();
  });

  it("removes a token an older version left in sessionStorage", async () => {
    sessionStorage.setItem("hokma.token", TOKEN);
    vi.resetModules();
    const fresh = await import("./token");
    expect(fresh.getToken()).toBeNull();
    expect(sessionStorage.getItem("hokma.token")).toBeNull();
  });

  it("takes the one-time owner code from #o= into memory only and removes both from the bar", () => {
    resetOwnerForTests();
    window.history.replaceState(null, "", `/app#t=${TOKEN}&o=${ONCE}`);
    initToken();
    expect(getToken()).toBe(TOKEN);
    expect(getOwnerOnce()).toBe(ONCE);
    expect(window.location.hash).toBe("");
    for (const store of [sessionStorage, localStorage]) {
      expect(JSON.stringify(Object.entries(store))).not.toContain(ONCE);      // never at rest
    }
    expect(document.cookie).not.toContain(ONCE);
    window.dispatchEvent(new Event("pagehide"));
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(ONCE);
    expect(window.name).not.toContain(ONCE);                                  // the code is not handed over either
    window.name = "";
  });

  it("ignores a malformed code and still removes it from the bar", () => {
    resetOwnerForTests();
    window.history.replaceState(null, "", `/#t=${TOKEN}&o=short<script>`);
    initToken();
    expect(getOwnerOnce()).toBeNull();
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

  it("sends the owner confirmation of this tab, 32 hex characters, on every call", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    await api.get("/api/x");
    await api.put("/api/profile/permission-mode", { mode: "ask" });
    const sent = fetchMock.mock.calls.map((c) => c[1].headers["X-Hokma-Owner"]);
    expect(sent[0]).toMatch(/^[0-9a-f]{32}$/);
    expect(sent[1]).toBe(sent[0]);
  });

  it("registers the confirmation and retries once when the engine asks for it", async () => {
    const refusal = () =>
      new Response(JSON.stringify({ detail: { code: "owner_confirmation_required", message: "no" } }), { status: 403 });
    fetchMock
      .mockResolvedValueOnce(refusal())
      .mockResolvedValueOnce(new Response(JSON.stringify({ registered: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ mode: "auto" }), { status: 200 }));
    expect(await api.put("/api/profile/permission-mode", { mode: "auto" })).toEqual({ mode: "auto" });
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      "/api/profile/permission-mode",
      "/api/session/owner-nonce",
      "/api/profile/permission-mode",
    ]);
    const body = JSON.parse(fetchMock.mock.calls[1]![1].body as string) as { nonce: string };
    expect(body.nonce).toBe(fetchMock.mock.calls[0]![1].headers["X-Hokma-Owner"]);
  });

  it("sends the one-time code with the first registration only, then forgets it", async () => {
    resetOwnerForTests();
    window.history.replaceState(null, "", `/#o=${ONCE}`);
    initToken();
    const refusal = () =>
      new Response(JSON.stringify({ detail: { code: "owner_confirmation_required", message: "no" } }), { status: 403 });
    fetchMock
      .mockResolvedValueOnce(refusal())
      .mockResolvedValueOnce(new Response(JSON.stringify({ registered: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }))
      .mockResolvedValueOnce(refusal())
      .mockResolvedValueOnce(new Response(JSON.stringify({ registered: true }), { status: 200 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    await api.put("/api/profile/permission-mode", { mode: "auto" });
    const first = JSON.parse(fetchMock.mock.calls[1]![1].body as string) as { nonce: string; once: string };
    expect(first.once).toBe(ONCE);
    expect(getOwnerOnce()).toBeNull();                                         // consumed by the accepted registration
    await api.put("/api/profile/permission-mode", { mode: "ask" });
    const second = JSON.parse(fetchMock.mock.calls[4]![1].body as string) as { nonce: string; once: string };
    expect(second.once).toBe("");
    expect(second.nonce).toBe(first.nonce);
    clearOwnerOnce();
  });

  it("keeps the code when the registration is refused, so a retry still has it", async () => {
    resetOwnerForTests();
    window.history.replaceState(null, "", `/#o=${ONCE}`);
    initToken();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "x", message: "no" } }), { status: 500 }));
    const { registerOwner } = await import("@/api");
    expect(await registerOwner()).toBe(false);
    expect(getOwnerOnce()).toBe(ONCE);
    clearOwnerOnce();
  });

  it("gives up with the engine's message when another tab already holds the confirmation", async () => {
    fetchMock.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/api/session/owner-nonce"
          ? new Response(JSON.stringify({ detail: { code: "owner_already_registered", message: "other" } }), { status: 409 })
          : new Response(JSON.stringify({ detail: { code: "owner_confirmation_required", message: "start again" } }), { status: 403 }),
      ),
    );
    await expect(api.put("/api/profile/permission-mode", { mode: "auto" })).rejects.toMatchObject({
      status: 403,
      code: "owner_confirmation_required",
      message: "start again",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);                                // the call, the registration, no loop
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
