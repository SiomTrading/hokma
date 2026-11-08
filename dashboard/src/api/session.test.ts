import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, clearToken, getToken, logoutSession, registerOwner, setToken } from "@/api";
import { getCatalog } from "@/i18n";
import { installBrowserKeys, storedDeviceRecord } from "@/test/fakeBrowserKeys";
import type { FakeIndexedDb } from "@/test/fakeBrowserKeys";
import { deviceKeySupported } from "./devicekey";
import { getOwnerNonce, resetOwnerForTests } from "./owner";
import {
  REFRESH_MS,
  enterWithCode,
  getSessionInfo,
  handoffToOwnHost,
  isStartOrigin,
  onEngineReconnected,
  refreshSession,
  resetSessionForTests,
  resumeSession,
  takeEntryCode,
} from "./session";

const TOKEN = "abcDEF1234567890_-xyz";
const OTHER_TOKEN = "zzzDEF1234567890_-abc";
const CODE = "entryCode_0123456789-abc";
const HOST = "hokma.localhost:8765";
const CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36";
const FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0";

interface PublicKey {
  x: string;
  y: string;
}

const fetchMock = vi.fn();
const answer = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
const callsTo = (path: string) => fetchMock.mock.calls.filter((c) => c[0] === path);
const resumeCalls = () => callsTo("/api/session/resume");
const bodyOf = (call: unknown[]) => JSON.parse((call[1] as { body: string }).body) as Record<string, unknown>;

type Route = (init?: RequestInit) => Response;
let routes: Record<string, Route>;
let challenges = 0;
let idb: FakeIndexedDb;

function defaultRoutes(): Record<string, Route> {
  return {
    "/api/session/enter": () => answer(200, { token: TOKEN }),
    "/api/session/challenge": () => answer(200, { challenge: `challenge-${++challenges}-abcdefgh` }),
    "/api/session/resume": () => answer(200, { token: TOKEN }),
    "/api/session/logout": () => answer(200, { logged_out: true }),
    "/api/session/owner-nonce": () => answer(200, { registered: true }),
    "/api/session/handoff-code": () => answer(200, { code: CODE, host: HOST }),
  };
}

/** Everything the page could have written to a place that survives it, other than the device key. */
function atRest(): string {
  return JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage), document.cookie, window.name]);
}

/** true when the signature (base64url, raw r||s) is the one of `message` made by the owner of `publicKey`. */
async function verifies(publicKey: PublicKey, message: string, signature: string): Promise<boolean> {
  const key = await crypto.subtle.importKey(
    "jwk",
    { kty: "EC", crv: "P-256", x: publicKey.x, y: publicKey.y },
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["verify"],
  );
  const raw = new Uint8Array(Buffer.from(signature, "base64url"));
  expect(raw.length).toBe(64);
  return crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, raw, new TextEncoder().encode(message));
}

/** A page just loaded on the engine's own host: nothing in memory, the key (if any) in IndexedDB. */
function newLoad(): void {
  clearToken();
  resetOwnerForTests();
  resetSessionForTests();
  fetchMock.mockClear();
}

/** The browser entered once with a code: the key is stored. Returns the public key the engine was given. */
async function rememberThisBrowser(): Promise<PublicKey> {
  expect(await enterWithCode(CODE)).toBe("ok");
  const sent = bodyOf(callsTo("/api/session/enter")[0]!).public_key as PublicKey;
  newLoad();
  return sent;
}

beforeEach(() => {
  clearToken();
  resetOwnerForTests();
  resetSessionForTests();
  challenges = 0;
  routes = defaultRoutes();
  fetchMock.mockReset();
  fetchMock.mockImplementation((path: string, init?: RequestInit) => {
    const route = routes[path];
    return Promise.resolve(route ? route(init) : answer(404, { detail: "unexpected" }));
  });
  vi.stubGlobal("fetch", fetchMock);
  idb = installBrowserKeys();
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(CHROME);
  window.name = "";
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetSessionForTests();
});

describe("entering with the handoff code on the engine's own host", () => {
  it("makes a non extractable key, keeps it, and gives the engine the code, a new confirmation and the public half", async () => {
    expect(await enterWithCode(CODE)).toBe("ok");
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/api/session/enter");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    const body = bodyOf(fetchMock.mock.calls[0]!);
    expect(Object.keys(body).sort()).toEqual(["code", "nonce", "public_key"]);
    expect(body.code).toBe(CODE);
    expect(body.nonce).toMatch(/^[0-9a-f]{32}$/);
    expect(body.nonce).toBe(getOwnerNonce());
    const publicKey = body.public_key as PublicKey;
    expect(publicKey.x).toMatch(/^[A-Za-z0-9_-]{43}$/);                       // 32 bytes, base64url
    expect(publicKey.y).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const stored = storedDeviceRecord(idb) as CryptoKeyPair;
    expect(stored.privateKey.extractable).toBe(false);
    expect(stored.privateKey.type).toBe("private");
    expect(stored.privateKey.usages).toEqual(["sign"]);
    const jwk = await crypto.subtle.exportKey("jwk", stored.publicKey);
    expect({ x: jwk.x, y: jwk.y }).toEqual(publicKey);
    await expect(crypto.subtle.exportKey("jwk", stored.privateKey)).rejects.toThrow();    // the private half cannot be read
    expect(getToken()).toBe(TOKEN);
    expect(getSessionInfo()).toEqual({ remembered: true, storageUnavailable: false, revoked: false });
  });

  it("takes the code out of the address and keeps the rest of it", () => {
    window.history.replaceState(null, "", `/chat?x=1#c=${CODE}&foo=bar`);
    expect(takeEntryCode()).toBe(CODE);
    expect(window.location.hash).toBe("#foo=bar");
    expect(window.location.pathname + window.location.search).toBe("/chat?x=1");
    window.history.replaceState(null, "", `/#c=${CODE}`);
    expect(takeEntryCode()).toBe(CODE);
    expect(window.location.hash).toBe("");
    expect(takeEntryCode()).toBeNull();
  });

  it("ignores a code that does not look like one, and still empties the address", () => {
    window.history.replaceState(null, "", "/#c=<script>");
    expect(takeEntryCode()).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("deletes the key again when the engine refuses the code", async () => {
    routes["/api/session/enter"] = () => answer(401, { detail: { code: "session.code_invalid", message: "no" } });
    expect(await enterWithCode(CODE)).toBe("denied");
    expect(storedDeviceRecord(idb)).toBeUndefined();
    expect(getToken()).toBeNull();
    expect(getSessionInfo().remembered).toBe(false);
  });

  it("reports an engine that cannot be reached, and keeps no key", async () => {
    routes["/api/session/enter"] = () => {
      throw new TypeError("failed");
    };
    expect(await enterWithCode(CODE)).toBe("unreachable");
    expect(storedDeviceRecord(idb)).toBeUndefined();
  });

  it("an answer without a valid token is a refusal", async () => {
    routes["/api/session/enter"] = () => answer(200, { token: "bad token<script>" });
    expect(await enterWithCode(CODE)).toBe("denied");
    expect(getToken()).toBeNull();
    expect(storedDeviceRecord(idb)).toBeUndefined();
  });

  it("enters without remembering when the browser refuses to store the key, and says so", async () => {
    idb.failWrite = true;
    expect(await enterWithCode(CODE)).toBe("ok");
    expect(getToken()).toBe(TOKEN);
    expect(getSessionInfo()).toEqual({ remembered: false, storageUnavailable: true, revoked: false });
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    vi.advanceTimersByTime(REFRESH_MS * 2);
    expect(callsTo("/api/session/challenge")).toHaveLength(0);               // nothing to renew without a stored key
  });

  it("enters without remembering when IndexedDB does not exist", async () => {
    vi.stubGlobal("indexedDB", undefined);
    expect(deviceKeySupported()).toBe(false);
    expect(await enterWithCode(CODE)).toBe("ok");
    expect(getSessionInfo()).toMatchObject({ remembered: false, storageUnavailable: true });
  });

  it("does not enter at all when WebCrypto does not exist", async () => {
    vi.stubGlobal("crypto", undefined);
    expect(await enterWithCode(CODE)).toBe("unsupported");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getSessionInfo().storageUnavailable).toBe(true);
  });

  it("refuses a code with a shape it never receives, without calling the engine", async () => {
    expect(await enterWithCode("short")).toBe("denied");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("the start link page hands over to the engine's own host", () => {
  const withSession = () => {
    setToken(TOKEN);
    return getOwnerNonce();
  };

  it("asks for the handoff code with the token and the confirmation and navigates to the host", async () => {
    const nonce = withSession();
    const navigate = vi.fn();
    expect(await handoffToOwnHost(navigate)).toBe(true);
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/api/session/handoff-code");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Hokma-Token"]).toBe(TOKEN);
    expect(init.headers["X-Hokma-Owner"]).toBe(nonce);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(`http://${HOST}/#c=${CODE}`);
    expect(atRest()).not.toContain(CODE);
  });

  it("does nothing in Firefox: the session stays here and is not remembered", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(FIREFOX);
    withSession();
    const navigate = vi.fn();
    expect(await handoffToOwnHost(navigate)).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("stays and flags the browser when it has no IndexedDB or no WebCrypto", async () => {
    withSession();
    vi.stubGlobal("indexedDB", undefined);
    const navigate = vi.fn();
    expect(await handoffToOwnHost(navigate)).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getSessionInfo().storageUnavailable).toBe(true);
  });

  it("does not navigate to a host that is not the engine's own", async () => {
    withSession();
    for (const host of ["evil.example.com", "127.0.0.1:8765", "hokma.localhost.evil.com:80", "hokma.localhost:80/x", "localhost:8765", "h-0a1b2c3d4e5f.localhost:8765", "other.localhost:8765"]) {
      routes["/api/session/handoff-code"] = () => answer(200, { code: CODE, host });
      const navigate = vi.fn();
      expect(await handoffToOwnHost(navigate), host).toBe(false);
      expect(navigate, host).not.toHaveBeenCalled();
    }
  });

  it("stays when the engine refuses or answers badly", async () => {
    withSession();
    const navigate = vi.fn();
    routes["/api/session/handoff-code"] = () => answer(403, { detail: { code: "owner_confirmation_required", message: "no" } });
    expect(await handoffToOwnHost(navigate)).toBe(false);
    routes["/api/session/handoff-code"] = () => answer(200, { host: HOST });
    expect(await handoffToOwnHost(navigate)).toBe(false);
    routes["/api/session/handoff-code"] = () => {
      throw new TypeError("failed");
    };
    expect(await handoffToOwnHost(navigate)).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("without a token there is nothing to hand over", async () => {
    expect(await handoffToOwnHost(vi.fn())).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("knows the page of the start link by its address", () => {
    expect(isStartOrigin("127.0.0.1")).toBe(true);
    expect(isStartOrigin(HOST.split(":")[0]!)).toBe(false);
    expect(isStartOrigin("localhost")).toBe(false);
  });

  it("registers the owner without asking for a cookie", async () => {
    setToken(TOKEN);
    expect(await registerOwner()).toBe(true);
    const body = bodyOf(callsTo("/api/session/owner-nonce")[0]!);
    expect(Object.keys(body).sort()).toEqual(["nonce", "once"]);
  });
});

describe("resume with the device key", () => {
  it("has nothing to do without a key: the entry screen, and no call to the engine", async () => {
    expect(await resumeSession()).toBe("nokey");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getToken()).toBeNull();
  });

  it("asks for a challenge, signs hokma-resume:<origin>:<challenge>:<nonce> and keeps the token in memory only", async () => {
    const publicKey = await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    const [challengePath, challengeInit] = fetchMock.mock.calls[0]!;
    expect(challengePath).toBe("/api/session/challenge");
    expect(challengeInit.method).toBe("POST");
    expect(challengeInit.credentials).toBe("same-origin");
    const [resumePath, resumeInit] = fetchMock.mock.calls[1]!;
    expect(resumePath).toBe("/api/session/resume");
    expect(resumeInit.credentials).toBe("same-origin");
    const body = bodyOf(fetchMock.mock.calls[1]!);
    expect(Object.keys(body).sort()).toEqual(["challenge", "nonce", "signature"]);
    expect(body.nonce).toMatch(/^[0-9a-f]{32}$/);
    expect(body.nonce).toBe(getOwnerNonce());
    expect(body.challenge).toBe("challenge-1-abcdefgh");
    expect(await verifies(publicKey, `hokma-resume:${window.location.origin}:${String(body.challenge)}:${String(body.nonce)}`, String(body.signature))).toBe(true);
    expect(await verifies(publicKey, `hokma-resume:${window.location.origin}:other:${String(body.nonce)}`, String(body.signature))).toBe(false);
    expect(await verifies(publicKey, `hokma-resume:http://hokma.localhost:9:${String(body.challenge)}:${String(body.nonce)}`, String(body.signature))).toBe(false);   // another origin, another message
    expect(getToken()).toBe(TOKEN);
    expect(getSessionInfo().remembered).toBe(true);
    for (const secret of [TOKEN, String(body.nonce), String(body.challenge), String(body.signature), CODE]) expect(atRest()).not.toContain(secret);
  });

  it("makes a new confirmation on every load and sends it to the later calls of the owner", async () => {
    await rememberThisBrowser();
    const first = getOwnerNonce();
    expect(await resumeSession()).toBe("ok");
    const nonce = bodyOf(resumeCalls()[0]!).nonce;
    expect(nonce).not.toBe(first);
    fetchMock.mockClear();
    routes["/api/x"] = () => answer(200, {});
    await api.get("/api/x");
    expect(fetchMock.mock.calls[0]![1].headers["X-Hokma-Owner"]).toBe(nonce);
  });

  it("goes to the entry screen on 401, keeps the key and shows no notice", async () => {
    await rememberThisBrowser();
    routes["/api/session/resume"] = () => answer(401, { detail: { code: "session.resume_denied", message: "no" } });
    expect(await resumeSession()).toBe("denied");
    expect(getToken()).toBeNull();
    expect(getSessionInfo().revoked).toBe(false);
    expect(storedDeviceRecord(idb)).toBeDefined();
  });

  it("a refused challenge is a refusal too", async () => {
    await rememberThisBrowser();
    routes["/api/session/challenge"] = () => answer(401, { detail: { code: "session.resume_denied", message: "no" } });
    expect(await resumeSession()).toBe("denied");
    expect(callsTo("/api/session/resume")).toHaveLength(0);
  });

  it("a revoked session says so on the entry screen and deletes the key", async () => {
    await rememberThisBrowser();
    routes["/api/session/resume"] = () => answer(401, { detail: { code: "session.revoked", message: "closed" } });
    expect(await resumeSession()).toBe("revoked");
    expect(getToken()).toBeNull();
    expect(getSessionInfo()).toEqual({ remembered: false, storageUnavailable: false, revoked: true });
    expect(storedDeviceRecord(idb)).toBeUndefined();
    expect(await resumeSession()).toBe("nokey");                              // the next load goes straight to the entry screen
  });

  it("a revocation announced by the challenge is handled the same way", async () => {
    await rememberThisBrowser();
    routes["/api/session/challenge"] = () => answer(401, { detail: { code: "session.revoked", message: "closed" } });
    expect(await resumeSession()).toBe("revoked");
    expect(storedDeviceRecord(idb)).toBeUndefined();
  });

  it("treats an unreachable engine, a server error or an answer without a valid token as no session", async () => {
    await rememberThisBrowser();
    routes["/api/session/challenge"] = () => {
      throw new TypeError("failed");
    };
    expect(await resumeSession()).toBe("unreachable");
    routes["/api/session/challenge"] = defaultRoutes()["/api/session/challenge"]!;
    routes["/api/session/resume"] = () => answer(500, {});
    expect(await resumeSession()).toBe("unreachable");
    routes["/api/session/resume"] = () => answer(200, { token: "bad token<script>" });
    expect(await resumeSession()).toBe("denied");
    routes["/api/session/challenge"] = () => answer(200, { nochallenge: true });
    expect(await resumeSession()).toBe("unreachable");
    expect(getToken()).toBeNull();
    expect(storedDeviceRecord(idb)).toBeDefined();                            // none of these deletes the key
  });

  it("an unreadable store is no key, and the page says the browser cannot keep one", async () => {
    await rememberThisBrowser();
    idb.failOpen = true;
    expect(await resumeSession()).toBe("nokey");
    expect(getSessionInfo().storageUnavailable).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a browser without IndexedDB goes to the entry screen without breaking", async () => {
    vi.stubGlobal("indexedDB", undefined);
    expect(await resumeSession()).toBe("nokey");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ignores a stored value that is not a non extractable pair", async () => {
    await rememberThisBrowser();
    idb.databases.get("hokma-session")!.get("keys")!.set("device", { privateKey: { type: "private", extractable: true }, publicKey: {} });
    expect(await resumeSession()).toBe("nokey");
    idb.databases.get("hokma-session")!.get("keys")!.set("device", "hello");
    expect(await resumeSession()).toBe("nokey");
  });
});

describe("refresh of an open tab", () => {
  const fakeIntervals = () => vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });

  it("repeats challenge and signature every 10 minutes with the same confirmation", async () => {
    fakeIntervals();
    const publicKey = await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    const nonce = bodyOf(resumeCalls()[0]!).nonce;
    vi.advanceTimersByTime(REFRESH_MS - 5000);
    expect(resumeCalls()).toHaveLength(1);
    vi.advanceTimersByTime(5000);
    await vi.waitFor(() => expect(resumeCalls()).toHaveLength(2));
    vi.advanceTimersByTime(REFRESH_MS);
    await vi.waitFor(() => expect(resumeCalls()).toHaveLength(3));
    expect(callsTo("/api/session/challenge")).toHaveLength(3);
    expect(resumeCalls().every((c) => bodyOf(c).nonce === nonce)).toBe(true);
    const used = resumeCalls().map((c) => bodyOf(c).challenge);
    expect(new Set(used).size).toBe(3);                                        // a new challenge each time
    const last = bodyOf(resumeCalls()[2]!);
    expect(await verifies(publicKey, `hokma-resume:${window.location.origin}:${String(last.challenge)}:${nonce as string}`, String(last.signature))).toBe(true);
    expect(getToken()).toBe(TOKEN);
  });

  it("starts after entering with a code only when the key was stored", async () => {
    fakeIntervals();
    expect(await enterWithCode(CODE)).toBe("ok");
    vi.advanceTimersByTime(REFRESH_MS);
    await vi.waitFor(() => expect(resumeCalls()).toHaveLength(1));
    expect(bodyOf(resumeCalls()[0]!).nonce).toBe(getOwnerNonce());
  });

  it("sends the tab back to the entry screen when the refresh finds the session revoked", async () => {
    await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    routes["/api/session/resume"] = () => answer(401, { detail: { code: "session.revoked", message: "closed" } });
    await refreshSession();
    expect(getToken()).toBeNull();
    expect(getSessionInfo().revoked).toBe(true);
    expect(storedDeviceRecord(idb)).toBeUndefined();
  });

  it("keeps the tab and stops asking when the refresh is simply denied", async () => {
    fakeIntervals();
    await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    routes["/api/session/resume"] = () => answer(401, { detail: { code: "session.resume_denied", message: "no" } });
    vi.advanceTimersByTime(REFRESH_MS);
    await vi.waitFor(() => expect(resumeCalls()).toHaveLength(2));
    await new Promise((done) => setTimeout(done, 100));                        // real time: the refusal is read and the timer stops
    expect(getToken()).toBe(TOKEN);
    vi.advanceTimersByTime(REFRESH_MS * 3);
    await new Promise((done) => setTimeout(done, 100));
    expect(resumeCalls()).toHaveLength(2);
  });

  it("a refresh with no key does nothing and does not touch the token", async () => {
    setToken(TOKEN);
    await refreshSession();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getToken()).toBe(TOKEN);
  });

  it("two refreshes at the same moment make one request", async () => {
    await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    fetchMock.mockClear();
    await Promise.all([refreshSession(), refreshSession()]);
    expect(callsTo("/api/session/challenge")).toHaveLength(1);
    expect(resumeCalls()).toHaveLength(1);
  });

  it("gives the confirmation back to the engine after the socket reconnects", async () => {
    await rememberThisBrowser();
    expect(await resumeSession()).toBe("ok");
    fetchMock.mockClear();
    onEngineReconnected();
    await vi.waitFor(() => expect(resumeCalls()).toHaveLength(1));
    expect(bodyOf(resumeCalls()[0]!).nonce).toBe(getOwnerNonce());
  });

  it("does not call the engine after a reconnect when no session was remembered", () => {
    onEngineReconnected();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("close the session of this browser", () => {
  it("calls logout with the token and the confirmation, deletes the key and goes back to the entry screen", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    expect(await enterWithCode(CODE)).toBe("ok");
    expect(storedDeviceRecord(idb)).toBeDefined();
    fetchMock.mockClear();
    await logoutSession();
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe("/api/session/logout");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Hokma-Token"]).toBe(TOKEN);
    expect(init.headers["X-Hokma-Owner"]).toBe(getOwnerNonce());
    expect(getToken()).toBeNull();
    expect(getSessionInfo()).toEqual({ remembered: false, storageUnavailable: false, revoked: false });
    expect(storedDeviceRecord(idb)).toBeUndefined();
    vi.advanceTimersByTime(REFRESH_MS * 2);                                    // the refresh stopped
    expect(callsTo("/api/session/challenge")).toHaveLength(0);
    newLoad();
    expect(await resumeSession()).toBe("nokey");
  });

  it("keeps the session and the key when the engine refuses to close it", async () => {
    expect(await enterWithCode(CODE)).toBe("ok");
    routes["/api/session/logout"] = () => answer(403, { detail: { code: "owner_confirmation_required", message: "no" } });
    await expect(logoutSession()).rejects.toMatchObject({ status: 403 });
    expect(getToken()).toBe(TOKEN);
    expect(storedDeviceRecord(idb)).toBeDefined();
  });

  it("logging out in a browser without IndexedDB does not fail", async () => {
    setToken(TOKEN);
    vi.stubGlobal("indexedDB", undefined);
    await logoutSession();
    expect(getToken()).toBeNull();
  });
});

describe("nothing at rest", () => {
  it("through enter, resume, refresh and logout no token, confirmation, code or challenge reaches any storage", async () => {
    const secrets: string[] = [CODE];
    expect(await enterWithCode(CODE)).toBe("ok");
    secrets.push(TOKEN, getOwnerNonce());
    newLoad();
    expect(await resumeSession()).toBe("ok");
    secrets.push(getOwnerNonce());
    await refreshSession();
    for (const secret of secrets) expect(atRest()).not.toContain(secret);
    // the only thing IndexedDB holds is the non extractable pair, under one key
    const databases = [...idb.databases.entries()];
    expect(databases.map(([name]) => name)).toEqual(["hokma-session"]);
    const stores = [...databases[0]![1].entries()];
    expect(stores.map(([name]) => name)).toEqual(["keys"]);
    const records = [...stores[0]![1].entries()];
    expect(records.map(([key]) => key)).toEqual(["device"]);
    const pair = records[0]![1] as CryptoKeyPair;
    expect(Object.keys(pair).sort()).toEqual(["privateKey", "publicKey"]);
    expect(pair.privateKey.extractable).toBe(false);
    expect(JSON.stringify(records)).not.toContain(TOKEN);
    await logoutSession();
    setToken(OTHER_TOKEN);
    for (const secret of [...secrets, OTHER_TOKEN]) expect(atRest()).not.toContain(secret);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(window.name).toBe("");
  });

  it("only devicekey.ts names IndexedDB, and no module of the session names another place that survives the page", () => {
    const strip = (file: string) => readFileSync(resolve(__dirname, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const file of ["session.ts", "client.ts", "owner.ts"]) expect(strip(file), file).not.toContain("indexedDB");
    for (const file of ["session.ts", "devicekey.ts"]) {
      for (const place of ["sessionStorage", "localStorage", "document.cookie", "window.name"]) {
        expect(strip(file), `${file} ${place}`).not.toContain(place);
      }
    }
    expect(strip("devicekey.ts")).not.toMatch(/\bput\([^)]*(token|nonce|code)/i);
    expect(strip("devicekey.ts")).toContain('DB_NAME = "hokma-session"');
    expect(strip("devicekey.ts")).toContain('STORE = "keys"');
    expect(strip("devicekey.ts")).toContain('RECORD = "device"');
    expect(strip("devicekey.ts")).toContain("generateKey({ name: \"ECDSA\", namedCurve: \"P-256\" }, false, [\"sign\"])");
  });
});

describe("R1-09 the entry screen does not send anyone to a terminal", () => {
  const BACKEND = resolve(__dirname, "../../../backend/hokma/catalog");
  const ENTRY_KEYS = /^(noToken\.|session\.|api\.unauthorized$|restart\.failedBody|ext\.noDatabase$|home\.notReady\.)/;

  it("no text of the dashboard catalog mentions `hokma start`, in es and en", () => {
    for (const locale of ["es", "en"] as const) {
      for (const [key, text] of Object.entries(getCatalog(locale))) {
        expect(text.toLowerCase(), `${locale} ${key}`).not.toMatch(/hokma start(?!s)/);
      }
    }
  });

  it("the texts of the entry screen and its failures do not mention the terminal", () => {
    for (const locale of ["es", "en"] as const) {
      for (const [key, text] of Object.entries(getCatalog(locale))) {
        if (ENTRY_KEYS.test(key)) expect(text.toLowerCase(), `${locale} ${key}`).not.toMatch(/terminal/);
      }
    }
  });

  it("the entry screen names the accesses of the desktop in both languages", () => {
    expect(getCatalog("es")["noToken.title"]).toMatch(/^Entra a Hokma desde el acceso Hokma de tu escritorio o del men. Inicio$/);
    expect(getCatalog("en")["noToken.title"]).toMatch(/Hokma shortcut/);
    expect(getCatalog("es")["noToken.body"]).not.toContain("Reiniciar Hokma");
    expect(getCatalog("en")["noToken.body"]).not.toContain("Restart Hokma");
    expect(getCatalog("es")["noToken.body"]).toContain("icono Hokma");
    expect(getCatalog("en")["noToken.body"]).toContain("Hokma icon");
    expect(getCatalog("es")["session.revokedNotice"]).toMatch(/copia vieja\. Entra de nuevo con el acceso Hokma$/);
    expect(getCatalog("en")["session.revokedNotice"]).toMatch(/old copy was used\. Enter again with the Hokma shortcut$/);
    expect(getCatalog("es")["session.firefoxNotice"]).toBe("Usa Chrome, Edge o Safari para que Hokma te recuerde");
    expect(getCatalog("en")["session.firefoxNotice"]).toBe("Use Chrome, Edge or Safari so that Hokma remembers you");
    expect(getCatalog("es")["session.noStorage"]).toMatch(/^Este navegador no permite guardar .* Hokma no te recordar./);
    expect(getCatalog("en")["session.noStorage"]).toBe("This browser does not allow saving the session key: Hokma will not remember you");
  });

  it("the owner messages of the backend catalogs do not mention `hokma start` or the terminal", () => {
    for (const locale of ["es", "en"]) {
      const catalog = JSON.parse(readFileSync(resolve(BACKEND, `${locale}.json`), "utf8")) as Record<string, string>;
      for (const key of Object.keys(catalog).filter((k) => k.startsWith("permissions.err.owner_") || k.startsWith("permissions.err.resume_") || k === "permissions.err.session_revoked")) {
        expect(catalog[key]!.toLowerCase(), `${locale} ${key}`).not.toMatch(/hokma start|terminal/);
      }
    }
  });
});
