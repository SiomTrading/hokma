import { useSyncExternalStore } from "react";

const STORAGE_KEY = "hokma.token";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{8,512}$/;

const listeners = new Set<() => void>();

function readStored(): string | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value && TOKEN_PATTERN.test(value) ? value : null;
  } catch {
    return null;
  }
}

let current: string | null = readStored();

function emit(): void {
  listeners.forEach((l) => l());
}

export function getToken(): string | null {
  return current;
}

export function setToken(token: string): void {
  if (!TOKEN_PATTERN.test(token)) return;
  current = token;
  try {
    localStorage.setItem(STORAGE_KEY, token);
  } catch {
    /* storage unavailable: the token lives only in memory */
  }
  emit();
}

export function clearToken(): void {
  current = null;
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
  emit();
}

/**
 * Reads the session token from `#t=` in the address bar, stores it and removes
 * it from the bar so it is not copied, bookmarked or leaked through history.
 */
export function initToken(): void {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return;
  const params = new URLSearchParams(hash);
  const fromUrl = params.get("t");
  if (fromUrl === null) return;
  if (TOKEN_PATTERN.test(fromUrl)) setToken(fromUrl);
  params.delete("t");
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
