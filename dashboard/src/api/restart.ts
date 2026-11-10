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
 * - "update": the panel started an update (task 3146). The engine goes down so a detached updater can replace it
 *   (a WebSocket closed with the reason `updating`, or the update answered `launched`). The screen says "Updating
 *   Hokma, it will restart by itself" and asks /health every second for up to 30 minutes. A new version reloads the
 *   page; the same version means the update failed, and the screen shows the engine's plain message with "Retry".
 * - "off": Hokma was shut down on purpose (this tab pressed Shut down Hokma, or a socket closed with the reason
 *   `shutting_down`). The tab says so and stays that way: it does not reconnect, ask /health or reload by itself.
 */
export type RestartPhase = "idle" | "restarting" | "failed" | "off";

export interface RestartState {
  phase: RestartPhase;
  /** Where the supervisor wrote why the engine could not start. */
  logPath: string | null;
  /** "update" while the detached updater replaces Hokma, and when it failed. */
  kind?: "restart" | "update";
  /** Why the update failed, in plain words from the engine. */
  message?: string | null;
}

/** What the update screen lends to the watch, so this module does not depend on the API client. */
export interface UpdateHooks {
  /** Why the last update failed, as the engine reports it, or null when it did not fail. */
  failure: () => Promise<string | null>;
  /** Starts the update again (the engine accepts it and goes down by itself). */
  retry: () => Promise<void>;
}

export const RESTART_REASON = "restarting";
export const SHUTDOWN_REASON = "shutting_down";
export const UPDATING_REASON = "updating";
export const POLL_MS = 1000;
export const GIVE_UP_MS = 60_000;
export const UPDATE_GIVE_UP_MS = 30 * 60_000;
const UPDATED_KEY = "hokma.updated";

let state: RestartState = { phase: "idle", logPath: null };
let knownLogPath: string | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let activeMode: WatchMode | null = null;
let hooks: UpdateHooks | null = null;
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

export function setUpdateHooks(next: UpdateHooks | null): void {
  hooks = next;
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

/** Version of the engine that served this page, learned once at start. A crash watch that sees the engine come
 * back with ANOTHER version (an update applied outside this tab: installer, another tab, `hokma update`) reloads
 * the page, otherwise the old code would keep running against the new engine. */
let pageEngineVersion: string | null = null;

export async function learnEngineVersion(): Promise<string | null> {
  if (pageEngineVersion) return pageEngineVersion;
  const alive = await probe();
  if (alive && !pageEngineVersion) pageEngineVersion = alive.version;
  return pageEngineVersion;
}

export function stopWatching(): void {
  if (timer) clearInterval(timer);
  timer = null;
  activeMode = null;
}

export type WatchMode = "restart" | "crash" | "update";

export interface WatchOptions {
  mode: WatchMode;
  /** The version the update installed. Without it, a restart is over once the engine went down and came back. */
  expectedVersion?: string | null;
  now?: () => number;
  reload?: () => void;
}

/**
 * Starts the watch. Safe to call from every socket that closes: while a watch of the same kind runs, a new call
 * is ignored (its 60 seconds must not start over each time a socket retries). A restart replaces a crash watch;
 * an update replaces both, and nothing replaces an update.
 */
export function watchEngine(options: WatchOptions): void {
  if (state.phase === "off") return;                              // a shutdown on purpose is not a crash
  const now = options.now ?? Date.now;
  const reload = () => reloadWithHandoff(options.reload);       // the session crosses the reload only this way
  if (timer && (activeMode === options.mode || activeMode === "update" || (activeMode === "restart" && options.mode === "crash"))) return;
  stopWatching();
  activeMode = options.mode;
  const updating = options.mode === "update";
  const giveUp = updating ? UPDATE_GIVE_UP_MS : GIVE_UP_MS;
  const startedAt = now();
  let sawDown = false;
  let busy = false;
  if (options.mode !== "crash") set({ phase: "restarting", logPath: knownLogPath, kind: updating ? "update" : "restart" });

  const tick = async (): Promise<void> => {
    if (busy) return;
    busy = true;
    try {
      const alive = await probe();
      if (!alive) sawDown = true;
      const back =
        alive !== null &&
        (options.mode === "crash" ||
          (updating ? sawDown : options.expectedVersion ? alive.version === options.expectedVersion : sawDown));
      if (back && alive) {
        stopWatching();
        const changed = pageEngineVersion !== null && alive.version !== pageEngineVersion;
        const installed = options.expectedVersion ? alive.version === options.expectedVersion : changed;
        if (updating && !installed) {
          // Hokma came back with the same version: the updater says why in the status the engine reads.
          const why = hooks ? await hooks.failure().catch(() => null) : null;
          if (why) {
            set({ phase: "failed", logPath: knownLogPath, kind: "update", message: why });
            return;
          }
        }
        if (options.mode !== "crash" || changed) {
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
      if (now() - startedAt >= giveUp) {
        stopWatching();
        set({ phase: "failed", logPath: knownLogPath, ...(updating ? { kind: "update" as const } : {}) });
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

/** The update failed before Hokma went down (the engine refused it): same screen, same "Retry". */
export function failUpdate(message: string): void {
  stopWatching();
  set({ phase: "failed", logPath: knownLogPath, kind: "update", message });
}

/** "Retry" on a failed update: start it again through the update screen's hook. */
export function retryUpdate(): void {
  const retry = hooks?.retry;
  dismissFailure();
  if (retry) void retry();
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
  pageEngineVersion = null;
  hooks = null;
  set({ phase: "idle", logPath: null });
}
