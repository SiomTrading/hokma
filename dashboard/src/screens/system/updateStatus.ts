import { useSyncExternalStore } from "react";
import { api, rememberLogPath } from "@/api";
import type { UpdateStateData } from "./types";

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

export function resetUpdateStatusForTests(): void {
  publish(null);
}
