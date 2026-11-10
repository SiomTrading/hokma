import { useSyncExternalStore } from "react";
import { api, ApiError, failUpdate, rememberLogPath, setUpdateHooks, watchEngine } from "@/api";
import type { UpdateResult, UpdateStateData } from "./types";

/** What the app knows about updates without asking the network: shared by the News screen and the dot. */
let current: UpdateStateData | null = null;
const listeners = new Set<() => void>();

function publish(next: UpdateStateData | null): void {
  current = next;
  listeners.forEach((l) => l());
}

export function getUpdateStatus(): UpdateStateData | null {
  return current;
}

export function useUpdateStatus(): UpdateStateData | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    getUpdateStatus,
    getUpdateStatus,
  );
}

/** Reads `/api/update/state` (no network search happens there). Errors leave the last known state. */
export async function refreshUpdateStatus(): Promise<UpdateStateData | null> {
  try {
    const next = await api.get<UpdateStateData>("/api/update/state");
    rememberLogPath(next.engine_log);
    publish(next);
    return next;
  } catch {
    return current;
  }
}

/**
 * Starts an update. The engine answers `launched`, starts the detached updater and goes down by itself; the tab
 * then waits for it ("Updating Hokma, it will restart by itself") and reconnects when it is back.
 */
export async function startUpdate(): Promise<UpdateResult> {
  const result = await api.post<UpdateResult>("/api/update", undefined, { timeoutMs: 120_000 });
  if (result.status === "launched") watchEngine({ mode: "update", expectedVersion: result.to_version });
  return result;
}

setUpdateHooks({
  failure: async () => {
    const next = await refreshUpdateStatus();
    return next?.phase === "error" && next.error ? next.error : null;
  },
  retry: async () => {
    try {
      await startUpdate();
    } catch (cause) {
      failUpdate(cause instanceof ApiError || cause instanceof Error ? cause.message : "");
    }
  },
});

export function resetUpdateStatusForTests(): void {
  publish(null);
}
