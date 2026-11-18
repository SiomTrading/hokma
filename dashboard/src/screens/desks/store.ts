import { useEffect, useSyncExternalStore } from "react";
import { ApiError } from "@/api";
import { t } from "@/i18n";
import { desksApi } from "./api";
import type { DeskSummary } from "./types";

/**
 * List of desks. It lives outside React so the sidebar rows and the Fund view (two component trees) read the same
 * data and a desk created in one place shows up in the other.
 */
export interface DesksSnapshot {
  desks: DeskSummary[];
  /** True once a load finished, with or without error. Tells "no desks yet" from "not loaded yet". */
  loaded: boolean;
  loading: boolean;
  /** User-safe message of the last failed load, or null. */
  error: string | null;
}

const EMPTY: DesksSnapshot = { desks: [], loaded: false, loading: false, error: null };

let snapshot: DesksSnapshot = EMPTY;
const listeners = new Set<() => void>();
let loadToken = 0;

function set(patch: Partial<DesksSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  listeners.forEach((fn) => fn());
}

export const subscribe = (fn: () => void): (() => void) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};
export const getSnapshot = (): DesksSnapshot => snapshot;

export function resetDesksStore(): void {
  loadToken += 1;
  snapshot = EMPTY;
  listeners.forEach((fn) => fn());
}

/** Reloads the list. A failed load keeps the previous list and records the message. */
export async function refreshDesks(): Promise<DeskSummary[] | null> {
  loadToken += 1;
  const token = loadToken;
  set({ loading: true, error: null });
  try {
    const desks = await desksApi.list();
    if (token !== loadToken) return null;
    const visible = desks.filter((desk) => desk.status !== "archived");
    set({ desks: visible, loaded: true, loading: false });
    return visible;
  } catch (cause) {
    if (token !== loadToken) return null;
    set({ loaded: true, loading: false, error: cause instanceof ApiError ? cause.message : t("desks.error.generic") });
    return null;
  }
}

/** Current list of desks. Loads it once on first use. */
export function useDesks(): DesksSnapshot {
  const state = useSyncExternalStore(subscribe, getSnapshot);
  useEffect(() => {
    const now = getSnapshot();
    if (!now.loaded && !now.loading) void refreshDesks();
  }, []);
  return state;
}
