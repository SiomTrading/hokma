import { useSyncExternalStore } from "react";
import { permissionsApi } from "./api";
import { isMode } from "./types";
import type { ModeChange, PermissionMode } from "./types";

/** The permission mode as the server has it, shared by the chat pill, the settings and the first-run card. */
export interface ModeSnapshot {
  /** True once the server answered with a valid mode. */
  loaded: boolean;
  mode: PermissionMode;
  introSeen: boolean;
  /** A turn was running when the mode changed: the new mode applies when it ends. */
  pending: boolean;
}

const INITIAL: ModeSnapshot = { loaded: false, mode: "ask", introSeen: true, pending: false };

let snapshot: ModeSnapshot = INITIAL;
let runningTabs = 0;
let inflight: Promise<void> | null = null;
const listeners = new Set<() => void>();

function set(next: Partial<ModeSnapshot>): void {
  snapshot = { ...snapshot, ...next };
  listeners.forEach((l) => l());
}

export function useModeSnapshot(): ModeSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}

/** Reads the mode from the server. Calls made while one is on the way share it. */
export function refreshMode(): Promise<void> {
  if (inflight) return inflight;
  inflight = permissionsApi
    .mode()
    .then(
      (data) => {
        if (isMode(data?.mode)) set({ loaded: true, mode: data.mode, introSeen: data.intro_seen === true });
      },
      () => undefined,
    )
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

function adopt(data: ModeChange): ModeChange {
  if (isMode(data?.mode)) {
    set({ loaded: true, mode: data.mode, introSeen: data.intro_seen === true, pending: data.changed && data.turns_running > 0 && runningTabs > 0 });
  }
  return data;
}

export async function applyMode(mode: PermissionMode): Promise<ModeChange> {
  return adopt(await permissionsApi.setMode({ mode }));
}

/** The owner read the one-time notice and does not want it: the mode stays as it is. */
export async function dismissIntro(): Promise<void> {
  const data = await permissionsApi.setMode({ intro_seen: true });
  if (isMode(data?.mode)) set({ loaded: true, mode: data.mode, introSeen: data.intro_seen === true });
}

/** A chat tab tells when its turn starts and ends; the "applies when the turn ends" note goes away with the last one. */
export function trackTurn(running: boolean): void {
  if (running) {
    runningTabs += 1;
    return;
  }
  runningTabs = Math.max(0, runningTabs - 1);
  if (runningTabs === 0 && snapshot.pending) set({ pending: false });
}

/** Tests: forget everything. */
export function resetModeStore(): void {
  snapshot = INITIAL;
  runningTabs = 0;
  inflight = null;
  listeners.forEach((l) => l());
}
