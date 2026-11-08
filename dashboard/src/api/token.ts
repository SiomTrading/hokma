import { useSyncExternalStore } from "react";

import { provideHandoff, takeHandoff } from "./handoff";
import { setOwnerOnce } from "./owner";

/**
 * The token lives in a variable of the page and in no storage of the browser (bug 1038): `sessionStorage` is also
 * written into the profile when the tab closes. To survive a reload it crosses through `window.name` (handoff.ts).
 * Older versions kept it in `localStorage` / `sessionStorage`: those copies are removed here.
 */
const STORAGE_KEY = "hokma.token";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{8,512}$/;

const listeners = new Set<() => void>();

function dropLegacy(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
}

function readStored(): string | null {
  dropLegacy();
  const value = takeHandoff("t");
  return value && TOKEN_PATTERN.test(value) ? value : null;
}

let current: string | null = readStored();

provideHandoff("t", () => current);

function emit(): void {
  listeners.forEach((l) => l());
}

/** true when this load came from the start link of Hokma (`#t=` and `#o=` together), not from a handoff of a reload. */
let fromStartLink = false;

export function startedFromLink(): boolean {
  return fromStartLink;
}

export function getToken(): string | null {
  return current;
}

export function setToken(token: string): void {
  if (!TOKEN_PATTERN.test(token)) return;
  current = token;
  emit();
}

export function clearToken(): void {
  current = null;
  emit();
}

/**
 * Reads the session token from `#t=` and the one-time owner code from `#o=`, stores the token (the code stays
 * in memory) and removes both from the bar so they are not copied, bookmarked or leaked through history.
 */
export function initToken(): void {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return;
  const params = new URLSearchParams(hash);
  const fromUrl = params.get("t");
  const once = params.get("o");
  if (fromUrl === null && once === null) return;
  if (fromUrl !== null && TOKEN_PATTERN.test(fromUrl)) setToken(fromUrl);
  if (once !== null) setOwnerOnce(once);             // memory only, used once by the registration
  if (fromUrl !== null && once !== null && TOKEN_PATTERN.test(fromUrl)) fromStartLink = true;
  params.delete("t");
  params.delete("o");
  const rest = params.toString();
  const clean = window.location.pathname + window.location.search + (rest ? `#${rest}` : "");
  window.history.replaceState(null, "", clean);
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useToken(): string | null {
  return useSyncExternalStore(subscribe, getToken, getToken);
}
