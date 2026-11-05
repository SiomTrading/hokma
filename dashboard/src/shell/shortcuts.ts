export type ShortcutId = "newChat" | "search" | "terminal" | "sidebar" | "rightPanel" | "fullscreen" | "settings" | "escape";

export interface ShortcutDef {
  id: ShortcutId;
  labelKey: string;
  /** Lowercase `event.key`. */
  key: string;
  /** Needs Ctrl (Cmd on macOS). */
  mod: boolean;
  shift?: boolean;
}

/** Single source of truth. Learn > Shortcuts lists this array. */
export const SHORTCUTS: readonly ShortcutDef[] = [
  { id: "newChat", labelKey: "shortcut.newChat", key: "n", mod: true },
  { id: "search", labelKey: "shortcut.search", key: "k", mod: true },
  { id: "terminal", labelKey: "shortcut.terminal", key: "`", mod: true },
  { id: "sidebar", labelKey: "shortcut.sidebar", key: "b", mod: true },
  { id: "rightPanel", labelKey: "shortcut.rightPanel", key: "j", mod: true },
  { id: "fullscreen", labelKey: "shortcut.fullscreen", key: "f", mod: true, shift: true },
  { id: "settings", labelKey: "shortcut.settings", key: ",", mod: true },
  { id: "escape", labelKey: "shortcut.escape", key: "escape", mod: false },
];

export function isMac(): boolean {
  return typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
}

export function getShortcut(id: ShortcutId): ShortcutDef {
  const def = SHORTCUTS.find((s) => s.id === id);
  if (!def) throw new Error(`Unknown shortcut: ${id}`);
  return def;
}

export function formatShortcut(def: ShortcutDef, mac: boolean = isMac()): string {
  if (def.key === "escape") return "Esc";
  const parts: string[] = [];
  if (def.mod) parts.push(mac ? "Cmd" : "Ctrl");
  if (def.shift) parts.push("Shift");
  parts.push(def.key.length === 1 ? def.key.toUpperCase() : def.key);
  return parts.join(" ");
}

/** Returns the shortcut a keyboard event triggers, or null. Escape is handled by the layers themselves. */
export function matchShortcut(event: Pick<KeyboardEvent, "key" | "code" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">, mac: boolean = isMac()): ShortcutId | null {
  const mod = mac ? event.metaKey : event.ctrlKey;
  if (!mod || event.altKey) return null;
  const key = event.key.toLowerCase();
  for (const def of SHORTCUTS) {
    if (!def.mod) continue;
    if (!!def.shift !== event.shiftKey) continue;
    if (def.key === key || (def.key === "`" && event.code === "Backquote")) return def.id;
  }
  return null;
}
