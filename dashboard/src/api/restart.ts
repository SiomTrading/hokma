import { useSyncExternalStore } from "react";

import { reloadWithHandoff } from "./handoff";

/**
 * What the tab does when the local engine goes away.
 *
 * - "restart": the engine announced a restart after an update (a WebSocket closed with the reason `restarting`,
 *   or the update answered with `restarting`). The screen says "Restarting Hokma..." at once, asks /health every
 *   second for up to 60 seconds and, when the new version answers, reloads the page on Settings > News.
 * - "crash": a socket dropped without a reason. Nothing is shown unless /health stays unreachable for 60 seconds
 *   (the supervisor normally brings the engine back within seconds and the sockets reconnect by themselves).
 * - "off": Hokma was shut down on purpose (this tab pressed Shut down Hokma, or a socket closed with the reason
 *   `shutting_down`). The tab says so and stays that way: it does not reconnect, ask /health or reload by itself.
 */
export type RestartPhase = "idle" | "restarting" | "failed" | "off";

export interface RestartState {
  phase: RestartPhase;
  /** Where the supervisor wrote why the engine could not start. */
  logPath: string | null;
}

export const RESTART_REASON = "restarting";
export const SHUTDOWN_REASON = "shutting_down";
export const POLL_MS = 1000;
export const GIVE_UP_MS = 60_000;
const UPDATED_KEY = "hokma.updated";

let state: RestartState = { phase: "idle", logPath: null };
let knownLogPath: string | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let activeMode: "restart" | "crash" | null = null;
const listeners = new Set<() => void>();

function set(next: RestartState): void {
  state = next;
  listeners.forEach((l) => l());
}

export function getRestartState(): RestartState {
  return state;
}

export function useRestartState(): RestartState {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    getRestartState,
    getRestartState,
  );
}

/** The update screen tells us where the engine log lives, so the failure message can name it. */
export function rememberLogPath(path: string | null): void {
  if (path) knownLogPath = path;
}

async function probe(): Promise<{ version: string } | null> {
  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), 900);
  try {
    const res = await fetch("/health", { signal: controller.signal, cache: "no-store" });
    if (!res.ok) return null;
    const body = (await res.json()) as { version?: unknown };
    return typeof body.version === "string" ? { version: body.version } : null;
  } catch {
    return null;
  } finally {
    clearTimeout(abort);
  }
}

export function stopWatching(): void {
  if (timer) clearInterval(timer);
  timer = null;
  activeMode = null;
}

export interface WatchOptions {
  mode: "restart" | "crash";
  /** The version the update installed. Without it, a restart is over once the engine went down and came back. */
  expectedVersion?: string | null;
  now?: () => number;
  reload?: () => void;
}

/**
 * Starts the watch. Safe to call from every socket that closes: while a watch of the same kind runs, a new call
 * is ignored (its 60 seconds must not start over each time a socket retries). A restart replaces a crash watch.
 */
export function watchEngine(options: WatchOptions): void {
  if (state.phase === "off") return;                              // a shutdown on purpose is not a crash
  const now = options.now ?? Date.now;
  const reload = () => reloadWithHandoff(options.reload);       // the session crosses the reload only this way
  if (timer && (activeMode === options.mode || activeMode === "restart")) return;
  stopWatching();
  activeMode = options.mode;
  const startedAt = now();
  let sawDown = false;
  let busy = false;
  if (options.mode === "restart") set({ phase: "restarting", logPath: knownLogPath });

  const tick = async (): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      const alive = await probe();
      if (!alive) sawDown = true;
      const back =
        alive !== null &&
        (options.mode === "crash" || (options.expectedVersion ? alive.version === options.expectedVersion : sawDown));
      if (back && alive) {
        stopWatching();
        if (options.mode === "restart") {
          try {
            sessionStorage.setItem(UPDATED_KEY, alive.version);
          } catch {
            /* storage unavailable: the page still reloads, only the banner is lost */
          }
          reload();
        } else {
          set({ phase: "idle", logPath: knownLogPath });
        }
        return;
      }
      if (now() - startedAt >= GIVE_UP_MS) {
        stopWatching();
        set({ phase: "failed", logPath: knownLogPath });
      }
    } finally {
      busy = false;
    }
  };
  timer = setInterval(() => void tick(), POLL_MS);
}

/** Hokma was shut down on purpose: this tab shows the shutdown screen for good. */
export function enterOff(): void {
  stopWatching();
  set({ phase: "off", logPath: knownLogPath });
}

export function isOff(): boolean {
  return state.phase === "off";
}

export function dismissFailure(): void {
  set({ phase: "idle", logPath: knownLogPath });
}

/** Version a restart just installed, read once when the page loads (the reload that follows a restart). */
export function takeJustUpdated(): string | null {
  try {
    const value = sessionStorage.getItem(UPDATED_KEY);
    sessionStorage.removeItem(UPDATED_KEY);
    return value;
  } catch {
    return null;
  }
}

export const justUpdated: string | null = takeJustUpdated();

/** Test helper: back to the initial state. */
export function resetRestartForTests(): void {
  stopWatching();
  knownLogPath = null;
  set({ phase: "idle", logPath: null });
}
