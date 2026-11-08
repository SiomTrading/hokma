/**
 * The remembered session (task 3063).
 *
 * Hokma remembers the browser with a device key, not with a value the page keeps. The key pair (ECDSA P-256, private
 * key not extractable) lives in IndexedDB (see devicekey.ts). The engine knows only its public half. Everything else
 * this module holds lives in variables of the page: the token, the owner confirmation and the codes are never written
 * to `sessionStorage`, `localStorage`, `document.cookie`, `window.name` or any file (bugs 1035 and 1038).
 *
 * Entering (once per browser):
 * - The start link opens on 127.0.0.1. There the page registers the owner as always, asks the engine for a one-time
 *   handoff code and navigates to the engine's own host name (`hokma.localhost:<port>/#c=<code>`).
 * - On that host the page makes the key pair, keeps it in IndexedDB, makes a new owner confirmation and gives the
 *   engine the code, the confirmation and the public key (`POST /api/session/enter`). The engine answers with the token.
 *
 * Coming back (F5, a new tab, the browser opened again) on that host with no token in memory:
 * - `POST /api/session/challenge` gives a challenge; the page signs `hokma-resume:<origin>:<challenge>:<nonce>` (the origin with its port) with the key and
 *   calls `POST /api/session/resume {nonce, challenge, signature}`. A server on another port with a copied cookie has
 *   no key to sign with, so it cannot do this.
 * - While the tab is open it repeats the challenge and the signature every 10 minutes with the same confirmation, and
 *   after the engine restarts by itself (an update).
 *
 * Without IndexedDB or WebCrypto (private modes, old browsers), and in Firefox, nothing is remembered and nothing breaks.
 */
import { useSyncExternalStore } from "react";

import { createDeviceKey, deleteDeviceKey, deviceKeySupported, loadDeviceKey, saveDeviceKey, signWithDeviceKey } from "./devicekey";
import { getOwnerNonce, renewOwnerNonce } from "./owner";
import { clearToken, getToken, setToken } from "./token";

export const REFRESH_MS = 10 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 8000;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{8,512}$/;
const CODE_PATTERN = /^[A-Za-z0-9_-]{8,512}$/;
const HOST_PATTERN = /^hokma\.localhost:[0-9]{1,5}$/;
const START_HOST = "127.0.0.1";

export type ResumeResult = "ok" | "denied" | "revoked" | "unreachable" | "nokey";
export type EnterResult = "ok" | "denied" | "unreachable" | "unsupported";

export interface SessionInfo {
  /** The engine knows the key of this browser and the page can sign with it: the session is remembered. */
  remembered: boolean;
  /** This browser cannot keep the key (no IndexedDB or WebCrypto, or it refused): the session is not remembered. */
  storageUnavailable: boolean;
  /** The engine closed the saved session (the engine said so, e.g. an old copy was used). */
  revoked: boolean;
}

const INITIAL: SessionInfo = { remembered: false, storageUnavailable: false, revoked: false };

let info: SessionInfo = INITIAL;
let timer: ReturnType<typeof setInterval> | null = null;
let refreshing: Promise<void> | null = null;
const listeners = new Set<() => void>();

function update(patch: Partial<SessionInfo>): void {
  info = { ...info, ...patch };
  listeners.forEach((l) => l());
}

/** Firefox keeps site data unencrypted on disk, so Hokma does not ask it to remember the session. */
export function isFirefox(): boolean {
  return typeof navigator !== "undefined" && /firefox|fxios/i.test(navigator.userAgent);
}

/** The page of the start link (127.0.0.1): it registers the owner and hands over to the engine's own host, nothing more. */
export function isStartOrigin(hostname: string = window.location.hostname): boolean {
  return hostname === START_HOST;
}

export function getSessionInfo(): SessionInfo {
  return info;
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useSessionInfo(): SessionInfo {
  return useSyncExternalStore(subscribe, getSessionInfo, getSessionInfo);
}

function stopRefresh(): void {
  if (timer !== null) clearInterval(timer);
  timer = null;
}

function startRefresh(): void {
  if (timer !== null) return;
  timer = setInterval(() => void refreshSession(), REFRESH_MS);
}

interface Answer {
  status: number;
  data: unknown;
}

/** A POST that answers with its status and body, or null when the engine could not be reached. */
async function post(path: string, body?: unknown, headers: Record<string, string> = {}): Promise<Answer | null> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* an answer without a JSON body */
    }
    return { status: res.status, data };
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

function errorCode(data: unknown): string {
  const code = (data as { detail?: { code?: unknown } } | null)?.detail?.code;
  return typeof code === "string" ? code : "";
}

function tokenOf(data: unknown): string | null {
  const token = (data as { token?: unknown } | null)?.token;
  return typeof token === "string" && TOKEN_PATTERN.test(token) ? token : null;
}

/**
 * Reads the handoff code (`#c=`) the engine's own host was opened with and removes it from the address at once, so it
 * is not copied, bookmarked or kept in the history. The code lives only in the value this returns.
 */
export function takeEntryCode(): string | null {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const code = params.get("c");
  if (code === null) return null;
  params.delete("c");
  const rest = params.toString();
  window.history.replaceState(null, "", window.location.pathname + window.location.search + (rest ? `#${rest}` : ""));
  return CODE_PATTERN.test(code) ? code : null;
}

/**
 * On the start link page, once the owner is registered: asks the engine for a one-time code and goes to the engine's
 * own host with it. Resolves true when the page is leaving. In Firefox, and where the browser cannot keep the key,
 * it does nothing: the session stays on 127.0.0.1 and is not remembered.
 */
export async function handoffToOwnHost(navigate: (url: string) => void = (url) => window.location.replace(url)): Promise<boolean> {
  const token = getToken();
  if (!token || isFirefox()) return false;
  if (!deviceKeySupported()) {
    update({ storageUnavailable: true });
    return false;
  }
  const res = await post("/api/session/handoff-code", undefined, { "X-Hokma-Token": token, "X-Hokma-Owner": getOwnerNonce() });
  if (res === null || res.status !== 200) return false;
  const { code, host } = (res.data ?? {}) as { code?: unknown; host?: unknown };
  if (typeof code !== "string" || !CODE_PATTERN.test(code)) return false;
  if (typeof host !== "string" || !HOST_PATTERN.test(host)) return false;
  navigate(`http://${host}/#c=${code}`);
  return true;
}

/**
 * On the engine's own host with a handoff code: makes the key pair, keeps it, makes a new owner confirmation and
 * enters. The token comes back in the answer and stays in memory. If the browser refuses to keep the key the session
 * still opens, but it is not remembered.
 */
export async function enterWithCode(code: string): Promise<EnterResult> {
  if (!CODE_PATTERN.test(code)) return "denied";
  if (typeof crypto === "undefined" || typeof crypto.subtle?.generateKey !== "function") {
    update({ storageUnavailable: true });
    return "unsupported";
  }
  let created: Awaited<ReturnType<typeof createDeviceKey>>;
  try {
    created = await createDeviceKey();
  } catch {
    update({ storageUnavailable: true });
    return "unsupported";
  }
  const stored = deviceKeySupported() && (await saveDeviceKey(created.pair));
  const res = await post("/api/session/enter", { code, nonce: renewOwnerNonce(), public_key: created.publicKey });
  const token = res !== null && res.status === 200 ? tokenOf(res.data) : null;
  if (token === null) {
    if (stored) await deleteDeviceKey();                         // the engine does not know this key: do not keep it
    return res === null || res.status >= 500 ? "unreachable" : "denied";
  }
  setToken(token);
  update({ remembered: stored, storageUnavailable: !stored, revoked: false });
  if (stored) startRefresh();
  return "ok";
}

async function closeRevoked(): Promise<void> {
  stopRefresh();
  await deleteDeviceKey();
  update({ remembered: false, revoked: true });
}

/** Challenge, signature and resume with the key of this browser. `nonce` is the owner confirmation the engine will hold. */
async function resumeWith(nonce: string): Promise<ResumeResult> {
  if (!deviceKeySupported()) return "nokey";
  let pair: CryptoKeyPair | null;
  try {
    pair = await loadDeviceKey();
  } catch {
    update({ storageUnavailable: true });
    return "nokey";
  }
  if (pair === null) return "nokey";

  const asked = await post("/api/session/challenge");
  if (asked === null) return "unreachable";
  if (asked.status === 401) {
    if (errorCode(asked.data) === "session.revoked") {
      await closeRevoked();
      return "revoked";
    }
    return "denied";
  }
  const challenge = (asked.data as { challenge?: unknown } | null)?.challenge;
  if (asked.status !== 200 || typeof challenge !== "string" || challenge.length === 0 || challenge.length > 512) return "unreachable";

  let signature: string;
  try {
    signature = await signWithDeviceKey(pair, `hokma-resume:${window.location.origin}:${challenge}:${nonce}`);
  } catch {
    await deleteDeviceKey();                                     // a key that cannot sign is of no use
    return "nokey";
  }

  const res = await post("/api/session/resume", { nonce, challenge, signature });
  if (res === null) return "unreachable";
  if (res.status === 200) {
    const token = tokenOf(res.data);
    if (token === null) return "denied";
    if (getToken() !== token) setToken(token);
    update({ remembered: true, revoked: false });
    startRefresh();
    return "ok";
  }
  if (res.status !== 401) return "unreachable";
  if (errorCode(res.data) === "session.revoked") {
    await closeRevoked();
    return "revoked";
  }
  return "denied";
}

/**
 * The page was loaded on the engine's own host with no token: proves who it is with the key of this browser and a new
 * 128 bit owner confirmation. "nokey" means this browser has no key: the entry screen shows. On success the token is
 * kept in memory, as always. A revoked answer is remembered so the entry screen can say why.
 */
export async function resumeSession(): Promise<ResumeResult> {
  return resumeWith(renewOwnerNonce());
}

/**
 * Renews the remembered session of an open tab (every 10 minutes, and after the engine came back) with the same owner
 * confirmation. A revocation sends the tab back to the entry screen. Any other refusal keeps the tab as it is, because
 * a tab that has no key (Firefox, private mode) has nothing to renew.
 */
export function refreshSession(): Promise<void> {
  if (refreshing === null) {
    refreshing = (async () => {
      const result = await resumeWith(getOwnerNonce());
      if (result === "revoked") clearToken();
      if (result === "denied" || result === "nokey") stopRefresh();
    })().finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

/** The page found the engine again after a lost connection: gives the confirmation of the owner back to the engine. */
export function onEngineReconnected(): void {
  if (info.remembered) void refreshSession();
}

/** The session of this browser was closed on purpose (or the engine closed it): forget what this module knows. */
export function forgetSession(): void {
  stopRefresh();
  update({ remembered: false, storageUnavailable: false, revoked: false });
}

/** Test helper: the state of a page that was just loaded. */
export function resetSessionForTests(): void {
  stopRefresh();
  refreshing = null;
  info = INITIAL;
  listeners.forEach((l) => l());
}
