import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { openSocket } from "@/api";
import type { Socket } from "@/api";
import { t } from "@/i18n";
import { isMac, matchShortcut } from "@/shell/shortcuts";
import { readTheme } from "./theme";
import type { LaunchOptions, ServerFrame, TermStatus, TermTabView } from "./types";

/**
 * Terminal sessions live here, outside React: hiding the dock keeps the shells running, and only closing a tab
 * (or the page) ends one. Each session owns an xterm instance in a detached host element that the panel moves
 * into view.
 */

interface Entry {
  id: string;
  n: number;
  term: Terminal;
  fit: FitAddon;
  host: HTMLDivElement;
  socket: Socket | null;
  status: TermStatus;
  cwd: string | null;
  command?: string;
  sawExit: boolean;
}

export interface TerminalSnapshot {
  tabs: readonly TermTabView[];
  activeId: string | null;
}

const MAX_TABS = 12;
const READLINE_KEYS = new Set(["k", "b", "j"]); // Ctrl K/B/J belong to the shell on Linux and Windows
const APP_SHORTCUTS = new Set(["terminal", "fullscreen", "settings", "newChat"]);

/** The xterm classes; tests swap them because jsdom cannot lay out a real terminal. */
const realDrivers = { Terminal, FitAddon, openSocket };
let drivers: typeof realDrivers = realDrivers;

export function setTerminalDrivers(next: typeof realDrivers | null): void {
  drivers = next ?? realDrivers;
}

let entries: Entry[] = [];
let activeId: string | null = null;
let counter = 0;
let activeProjectId: string | null = null;
let autoStarted = false;
let snapshot: TerminalSnapshot = { tabs: [], activeId: null };
const listeners = new Set<() => void>();
let themeSource: Element | null = null;

function publish(): void {
  snapshot = {
    tabs: entries.map((e) => ({ id: e.id, n: e.n, status: e.status, cwd: e.cwd })),
    activeId,
  };
  for (const listener of listeners) listener();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSnapshot(): TerminalSnapshot {
  return snapshot;
}

export function setActiveProject(id: string | null): void {
  activeProjectId = id;
}

/** True the first time it is called: the dock opens one terminal by itself, once. */
export function takeAutoStart(): boolean {
  if (autoStarted || entries.length > 0) return false;
  autoStarted = true;
  return true;
}

export function setThemeSource(element: Element | null): void {
  themeSource = element;
  applyTheme();
}

export function applyTheme(): void {
  if (!themeSource) return;
  const theme = readTheme(themeSource);
  for (const entry of entries) entry.term.options.theme = theme;
}

function setStatus(entry: Entry, status: TermStatus): void {
  entry.status = status;
  publish();
}

function sendResize(entry: Entry): void {
  entry.socket?.send({ type: "resize", cols: entry.term.cols, rows: entry.term.rows });
}

function onFrame(entry: Entry, frame: ServerFrame): void {
  switch (frame.type) {
    case "ready":
      entry.cwd = frame.cwd;
      setStatus(entry, "ready");
      sendResize(entry);
      if (entry.command) {
        entry.socket?.send({ type: "input", data: `${entry.command}\r` });
        entry.command = undefined;
      }
      break;
    case "output":
      entry.term.write(frame.data);
      break;
    case "exit":
      entry.sawExit = true;
      entry.term.write(`\r\n\x1b[2m${frame.code === null ? t("term.exitedNoCode") : t("term.exited", { code: String(frame.code) })}\x1b[0m\r\n`);
      setStatus(entry, "exited");
      break;
    case "error": {
      entry.sawExit = true;
      const key = `term.error.${frame.code}`;
      const text = frame.code === "too_many_terminals" || frame.code === "pty_unavailable" || frame.code === "spawn_failed" ? t(key) : t("term.error.unknown");
      entry.term.write(`\r\n\x1b[31m${text}\x1b[0m\r\n`);
      setStatus(entry, "error");
      break;
    }
    default:
      break;
  }
}

function copySelection(term: Terminal): void {
  const text = term.getSelection();
  if (text && navigator.clipboard) void navigator.clipboard.writeText(text).catch(() => undefined);
}

/** Copy and paste, and the keys that must reach the app or the shell. Returns false = xterm must not handle it. */
export function handleKey(term: Terminal, event: KeyboardEvent, mac: boolean = isMac()): boolean {
  if (event.type !== "keydown") return true;
  const key = event.key.toLowerCase();
  const copy = mac ? event.metaKey && key === "c" : event.ctrlKey && event.shiftKey && key === "c";
  const paste = mac ? event.metaKey && key === "v" : event.ctrlKey && event.shiftKey && key === "v";
  if (copy) {
    copySelection(term);
    return false;
  }
  if (paste) return false; // the browser fires a paste event that xterm turns into input
  if (!mac && event.ctrlKey && !event.shiftKey && !event.altKey && key === "c" && term.hasSelection()) {
    copySelection(term); // Ctrl C with a selection copies; without one it interrupts the command
    return false;
  }
  const shortcut = matchShortcut(event, mac);
  if (shortcut && APP_SHORTCUTS.has(shortcut)) return false; // bubbles to the app
  if (!mac && shortcut && READLINE_KEYS.has(key) && !event.shiftKey) {
    event.stopPropagation(); // the shell gets it, the app shortcut does not fire
    return true;
  }
  return true;
}

export function launchTerminal(options: LaunchOptions = {}): string | null {
  if (entries.length >= MAX_TABS) return null;
  counter += 1;
  const id = `term-${counter}`;
  const host = document.createElement("div");
  host.className = "term-host";
  const term = new drivers.Terminal({
    fontFamily: '"JetBrains Mono", ui-monospace, monospace',
    fontSize: 12,
    lineHeight: 1.35,
    cursorBlink: true,
    scrollback: 5000,
    allowTransparency: true,
    theme: readTheme(themeSource),
  });
  const fit = new drivers.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  const entry: Entry = {
    id,
    n: counter,
    term,
    fit,
    host,
    socket: null,
    status: "connecting",
    cwd: null,
    command: options.command,
    sawExit: false,
  };
  term.attachCustomKeyEventHandler((event) => handleKey(term, event));
  term.onData((data) => entry.socket?.send({ type: "input", data }));
  term.onResize(() => sendResize(entry));

  const projectId = options.projectId === undefined ? activeProjectId : options.projectId;
  const query = new URLSearchParams({ cols: String(term.cols), rows: String(term.rows) });
  if (projectId) query.set("project_id", projectId);
  entry.socket = drivers.openSocket(`/ws/terminal?${query.toString()}`, {
    reconnect: false,
    onMessage: (data) => {
      if (data && typeof data === "object" && "type" in data) onFrame(entry, data as ServerFrame);
    },
    onClose: () => {
      if (!entries.includes(entry)) return;
      if (entry.status === "connecting" || entry.status === "ready") {
        if (!entry.sawExit) entry.term.write(`\r\n\x1b[31m${t("term.disconnected")}\x1b[0m\r\n`);
        setStatus(entry, "closed");
      }
    },
  });
  entries = [...entries, entry];
  activeId = id;
  publish();
  return id;
}

export function activate(id: string): void {
  if (entries.some((e) => e.id === id) && activeId !== id) {
    activeId = id;
    publish();
  }
}

export function closeTerminal(id: string): void {
  const index = entries.findIndex((e) => e.id === id);
  if (index < 0) return;
  const [entry] = entries.splice(index, 1);
  entries = [...entries];
  entry.socket?.close(); // the server hangs up and kills the shell
  entry.term.dispose();
  entry.host.remove();
  if (activeId === id) activeId = entries[Math.min(index, entries.length - 1)]?.id ?? null;
  publish();
}

export function hostOf(id: string | null): { host: HTMLDivElement; fit: () => void; focus: () => void } | null {
  const entry = entries.find((e) => e.id === id);
  if (!entry) return null;
  return {
    host: entry.host,
    fit: () => {
      try {
        entry.fit.fit();
      } catch {
        /* host not laid out yet */
      }
    },
    focus: () => entry.term.focus(),
  };
}

export function titleOf(id: string): string {
  const entry = entries.find((e) => e.id === id);
  return entry ? t("term.tab", { n: String(entry.n) }) : "";
}

/** Test helper: close every session and forget the auto start. */
export function resetTerminals(): void {
  for (const entry of [...entries]) closeTerminal(entry.id);
  entries = [];
  activeId = null;
  counter = 0;
  autoStarted = false;
  activeProjectId = null;
  themeSource = null;
  publish();
}
