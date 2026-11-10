import { parseChatHash } from "./chatRoute";
import type { TabParams } from "./registry";
import type { SidebarState, Tab } from "./store";

/**
 * What is remembered where (SPEC-001 addendum 0.0.7, 2.3):
 * - `sessionStorage["hokma.tabs.v1"]`: the panel tabs of THIS browser tab. A second browser tab has its own.
 * - `localStorage["hokma.layout.v1"]`: sidebar, right panel and terminal, shared (a preference of the person).
 * - `localStorage["hokma.ui.lastTabs.v1"]`: the last picture of the panel tabs, read only to seed a browser tab
 *   that is born empty.
 * - `localStorage["hokma.ui.v1"]`: the old single key, read once to fill the two above and then removed.
 * The session token and the owner confirmation never go through here.
 */
export const TABS_KEY = "hokma.tabs.v1";
export const LAYOUT_KEY = "hokma.layout.v1";
export const LAST_TABS_KEY = "hokma.ui.lastTabs.v1";
export const LEGACY_KEY = "hokma.ui.v1";

export interface LayoutPrefs {
  sidebar: SidebarState;
  rightPanel: boolean;
  terminal: boolean;
}

export interface Startup {
  tabs: Tab[];
  activeKey: string | null;
  layout: LayoutPrefs;
}

const DEFAULT_LAST_TABS: Tab[] = [{ key: "panel", screen: "panel", params: {} }];

let draftCounter = 0;

/** A draft id is also the `client_ref` of its first message: 1 to 64 characters of `[A-Za-z0-9_-]`. */
export function newDraftId(): string {
  const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : null;
  if (random) return random;
  draftCounter += 1;
  return `d${draftCounter}-${Date.now().toString(36)}`;
}

export function tabKeyFor(screen: string, params: TabParams = {}): string {
  const entries = Object.entries(params).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return screen;
  return `${screen}:${entries.map(([k, v]) => `${k}=${v}`).join("&")}`;
}

export function draftTab(id: string = newDraftId()): Tab {
  const params = { draft: id };
  return { key: tabKeyFor("chat", params), screen: "chat", params };
}

export const isDraft = (tab: Tab): boolean => tab.screen === "chat" && typeof tab.params.draft === "string";

/** The stored conversation a chat tab shows: its own param, or the one a draft was bound to. */
export function conversationOf(tab: Tab): string | null {
  if (tab.screen !== "chat") return null;
  return tab.params.conversation ?? tab.conversation ?? null;
}

function isParams(value: unknown): value is TabParams {
  return !!value && typeof value === "object" && Object.values(value).every((v) => typeof v === "string");
}

function cleanTabs(value: unknown): Tab[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const out: Tab[] = [];
  for (const raw of value) {
    const t = raw as Partial<Tab> | null;
    if (!t || typeof t.screen !== "string" || typeof t.key !== "string" || !isParams(t.params) || seen.has(t.key)) continue;
    seen.add(t.key);
    out.push({
      key: t.key,
      screen: t.screen,
      params: t.params,
      ...(typeof t.title === "string" ? { title: t.title } : {}),
      ...(typeof t.conversation === "string" ? { conversation: t.conversation } : {}),
    });
  }
  return out;
}

/** A bound draft is saved as the stored conversation it became, so a reload brings it back with its history. */
export function persistable(tabs: readonly Tab[]): Tab[] {
  const out: Tab[] = [];
  for (const tab of tabs) {
    if (isDraft(tab) && tab.conversation) {
      const params = { conversation: tab.conversation };
      const key = tabKeyFor("chat", params);
      if (!out.some((t) => t.key === key)) out.push({ key, screen: "chat", params, ...(tab.title ? { title: tab.title } : {}) });
    } else if (!out.some((t) => t.key === tab.key)) {
      const { conversation: _bound, ...rest } = tab;
      out.push(rest);
    }
  }
  return out;
}

function readJson(storage: Storage | null, key: string): unknown {
  try {
    const raw = storage?.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function safeStorage(kind: "session" | "local"): Storage | null {
  try {
    return kind === "session" ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

function layoutOf(data: Record<string, unknown> | null): LayoutPrefs {
  return {
    sidebar: data?.sidebar === 1 || data?.sidebar === 2 ? data.sidebar : 0,
    rightPanel: typeof data?.rightPanel === "boolean" ? data.rightPanel : true,
    terminal: typeof data?.terminal === "boolean" ? data.terminal : true,
  };
}

/**
 * Fills `hokma.layout.v1` and `hokma.ui.lastTabs.v1` from the old `hokma.ui.v1` once, and removes it. An old plain
 * "chat" tab (it had no params) becomes a draft. Nothing is copied when either new key exists already.
 */
export function migrateLegacy(local: Storage | null = safeStorage("local")): void {
  if (!local) return;
  try {
    if (local.getItem(LEGACY_KEY) === null) return;
    if (local.getItem(LAYOUT_KEY) === null && local.getItem(LAST_TABS_KEY) === null) {
      const data = readJson(local, LEGACY_KEY) as Record<string, unknown> | null;
      const tabs = cleanTabs(data?.tabs).map((tab) => (tab.screen === "chat" && Object.keys(tab.params).length === 0 ? draftTab() : tab));
      local.setItem(LAYOUT_KEY, JSON.stringify(layoutOf(data)));
      local.setItem(LAST_TABS_KEY, JSON.stringify(tabs));
    }
    local.removeItem(LEGACY_KEY);
  } catch {
    /* storage unavailable or full */
  }
}

export function loadLayout(local: Storage | null = safeStorage("local")): LayoutPrefs {
  return layoutOf(readJson(local, LAYOUT_KEY) as Record<string, unknown> | null);
}

function readLastTabs(local: Storage | null): Tab[] | null {
  const raw = readJson(local, LAST_TABS_KEY);
  if (!Array.isArray(raw)) return null;
  return cleanTabs(raw).filter((tab) => !isDraft(tab) && !(tab.screen === "chat" && Object.keys(tab.params).length === 0));
}

function withConversation(tabs: Tab[], id: string): { tabs: Tab[]; key: string } {
  const found = tabs.find((tab) => conversationOf(tab) === id);
  if (found) return { tabs, key: found.key };
  const params = { conversation: id };
  const key = tabKeyFor("chat", params);
  return { tabs: [...tabs, { key, screen: "chat", params }], key };
}

/**
 * The panel tabs a browser tab starts with (table 2.4): its own saved state when it has one, else the one named
 * by the address, else the last picture of the tabs plus a new draft.
 */
export function startupTabs(
  hash: string = typeof window === "undefined" ? "" : window.location.hash,
  session: Storage | null = safeStorage("session"),
  local: Storage | null = safeStorage("local"),
): Startup {
  migrateLegacy(local);
  const layout = loadLayout(local);
  const route = parseChatHash(hash);
  const saved = readJson(session, TABS_KEY) as { tabs?: unknown; activeKey?: unknown } | null;
  const own = saved ? cleanTabs(saved.tabs) : [];

  if (own.length > 0) {
    const active = typeof saved?.activeKey === "string" && own.some((t) => t.key === saved.activeKey) ? saved.activeKey : own[own.length - 1]!.key;
    if (route?.kind === "conversation") {
      const next = withConversation(own, route.id);
      return { tabs: next.tabs, activeKey: next.key, layout };
    }
    if (route?.kind === "new") {
      const current = own.find((t) => t.key === active);
      if (current && isDraft(current) && !current.conversation) return { tabs: own, activeKey: active, layout };
      const draft = draftTab();
      return { tabs: [...own, draft], activeKey: draft.key, layout };
    }
    return { tabs: own, activeKey: active, layout };
  }

  if (route?.kind === "new") {
    const draft = draftTab();
    return { tabs: [draft], activeKey: draft.key, layout };
  }
  if (route?.kind === "conversation") {
    const next = withConversation([], route.id);
    return { tabs: next.tabs, activeKey: next.key, layout };
  }
  const last = readLastTabs(local) ?? DEFAULT_LAST_TABS;
  const draft = draftTab();
  return { tabs: [...last, draft], activeKey: draft.key, layout };
}

export function saveTabs(tabs: readonly Tab[], activeKey: string | null, session: Storage | null = safeStorage("session")): void {
  try {
    const bound = tabs.find((t) => t.key === activeKey);
    const active = bound && isDraft(bound) && bound.conversation ? tabKeyFor("chat", { conversation: bound.conversation }) : activeKey;
    session?.setItem(TABS_KEY, JSON.stringify({ tabs: persistable(tabs), activeKey: active }));
  } catch {
    /* storage unavailable */
  }
}

/** Last picture for a browser tab that is born empty: no drafts (they have nothing to bring back). */
export function saveLastTabs(tabs: readonly Tab[], local: Storage | null = safeStorage("local")): void {
  try {
    local?.setItem(LAST_TABS_KEY, JSON.stringify(persistable(tabs).filter((tab) => !isDraft(tab))));
  } catch {
    /* storage unavailable */
  }
}

export function saveLayout(layout: LayoutPrefs, local: Storage | null = safeStorage("local")): void {
  try {
    local?.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    /* storage unavailable */
  }
}
