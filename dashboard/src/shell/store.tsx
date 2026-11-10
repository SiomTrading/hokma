import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ToastItem, ToastTone } from "@/design";
import { getLocale, setLocale as setI18nLocale } from "@/i18n";
import type { Locale } from "@/i18n";
import type { TabParams } from "./registry";
import { conversationOf, draftTab, isDraft, newDraftId, saveLastTabs, saveLayout, saveTabs, startupTabs, tabKeyFor } from "./tabsStorage";

export { tabKeyFor };

export type Theme = "dark" | "light";
export type SidebarState = 0 | 1 | 2; // 0 expanded, 1 icons only, 2 hidden

export interface Tab {
  key: string;
  screen: string;
  params: TabParams;
  /** Runtime override set by the screen (e.g. conversation name). */
  title?: string;
  /** A draft chat tab that the engine has given a stored conversation: the key stays, the screen is not remounted. */
  conversation?: string;
}

export interface Layout {
  sidebar: SidebarState;
  rightPanel: boolean;
  terminal: boolean;
  focus: boolean;
}

export interface ToastInput {
  tone?: ToastTone;
  text: string;
  /** Milliseconds before it disappears. Default 5000. */
  durationMs?: number;
}

export interface ShellApi {
  tabs: readonly Tab[];
  activeKey: string | null;
  /**
   * Opens a screen, or activates the tab that already shows it. For "chat": `{ conversation }` finds the tab that shows
   * that conversation (also a draft bound to it) or opens one; `{ draft }` opens that draft; without params it goes to
   * the chat tab used last, or opens a draft when there is none.
   */
  openScreen: (screen: string, params?: TabParams) => void;
  closeTab: (key: string) => void;
  activateTab: (key: string) => void;
  setTabTitle: (key: string, title: string | undefined) => void;
  /** Ties a draft chat tab to the conversation the engine created for it, without changing its key or remounting it. */
  bindConversation: (key: string, conversationId: string) => void;
  /**
   * Changes the params of an open tab (the chart's symbol). The key follows the params, so the tab stays reachable by
   * `openScreen` with the same params, and the active tab stays active under its new key. When another tab already has
   * those params, the two are merged: this one closes and the other one takes its place (and its focus). Returns the
   * key the tab has now, or null when there is no tab with `key`. The screen is mounted again under the new key.
   */
  setTabParams: (key: string, params: TabParams) => string | null;

  layout: Layout;
  toggleSidebar: () => void;
  toggleRightPanel: () => void;
  toggleTerminal: () => void;
  toggleFocus: () => void;

  theme: Theme;
  setTheme: (theme: Theme) => void;
  locale: Locale;
  setLocale: (locale: Locale) => void;

  settings: { open: boolean; section: string };
  openSettings: (section?: string) => void;
  closeSettings: () => void;

  sidebarQuery: string;
  setSidebarQuery: (query: string) => void;
  focusSearch: () => void;
  newConversation: () => void;

  toasts: readonly ToastItem[];
  toast: (input: ToastInput) => void;
  dismissToast: (id: number) => void;

  emit: (name: string, payload?: unknown) => void;
  on: (name: string, handler: (payload: unknown) => void) => () => void;
}

const THEME_KEY = "hokma.theme";

function loadTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

const ShellContext = createContext<ShellApi | null>(null);

export function ShellProvider({ children }: { children: ReactNode }) {
  const initial = useRef<ReturnType<typeof startupTabs> | null>(null);
  if (initial.current === null) initial.current = startupTabs();
  const start = initial.current;

  const [tabs, setTabs] = useState<Tab[]>(start.tabs);
  const [activeKey, setActiveKey] = useState<string | null>(start.activeKey);
  const [sidebar, setSidebar] = useState<SidebarState>(start.layout.sidebar);
  const [rightPanel, setRightPanel] = useState(start.layout.rightPanel);
  const [terminal, setTerminal] = useState(start.layout.terminal);
  const [focus, setFocus] = useState(false);
  const [theme, setThemeState] = useState<Theme>(loadTheme);
  const [locale, setLocaleState] = useState<Locale>(getLocale);
  const [settings, setSettings] = useState({ open: false, section: "general" });
  const [sidebarQuery, setSidebarQuery] = useState("");
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const listeners = useRef(new Map<string, Set<(payload: unknown) => void>>());
  const toastId = useRef(0);
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;
  const sidebarRef = useRef(sidebar);
  sidebarRef.current = sidebar;
  const focusRef = useRef(focus);
  focusRef.current = focus;

  // Tabs and the active one belong to this browser tab (sessionStorage); the last picture is only a seed for a new one.
  const firstSave = useRef(true);
  useEffect(() => {
    saveTabs(tabs, activeKey);
    if (!firstSave.current) saveLastTabs(tabs);
    firstSave.current = false;
  }, [tabs, activeKey]);
  useEffect(() => {
    saveLayout({ sidebar, rightPanel, terminal });
  }, [sidebar, rightPanel, terminal]);

  // The chat tab used last: what the "Chat" entry of the sidebar goes back to.
  const lastChatKey = useRef<string | null>(null);
  useEffect(() => {
    const active = tabs.find((t) => t.key === activeKey);
    if (active?.screen === "chat") lastChatKey.current = active.key;
  }, [tabs, activeKey]);

  const emit = useCallback((name: string, payload?: unknown) => {
    listeners.current.get(name)?.forEach((handler) => handler(payload));
  }, []);

  const on = useCallback((name: string, handler: (payload: unknown) => void) => {
    const set = listeners.current.get(name) ?? new Set();
    set.add(handler);
    listeners.current.set(name, set);
    return () => {
      set.delete(handler);
    };
  }, []);

  const openScreen = useCallback((screen: string, params: TabParams = {}) => {
    if (screen === "chat" && Object.keys(params).length === 0) {
      const chats = tabsRef.current.filter((t) => t.screen === "chat");
      const last = chats.find((t) => t.key === lastChatKey.current) ?? chats[chats.length - 1];
      if (last) {
        setActiveKey(last.key);
        return;
      }
      params = { draft: newDraftId() };
    }
    let key = tabKeyFor(screen, params);
    const conversation = screen === "chat" ? params.conversation : undefined;
    if (conversation) {
      key = tabsRef.current.find((t) => conversationOf(t) === conversation)?.key ?? key;
    }
    if (!tabsRef.current.some((t) => t.key === key)) {
      const next = [...tabsRef.current, { key, screen, params }];
      tabsRef.current = next;
      setTabs(next);
    }
    setActiveKey(key);
  }, []);

  const closeTab = useCallback((key: string) => {
    const rest = tabsRef.current.filter((t) => t.key !== key);
    setTabs(rest);
    setActiveKey((active) => (active === key ? (rest[rest.length - 1]?.key ?? null) : active));
  }, []);

  const setTabTitle = useCallback((key: string, title: string | undefined) => {
    setTabs((current) => current.map((t) => (t.key === key && t.title !== title ? { ...t, title } : t)));
  }, []);

  const bindConversation = useCallback((key: string, conversationId: string) => {
    const current = tabsRef.current;
    if (!current.some((t) => t.key === key && isDraft(t) && t.conversation !== conversationId)) return;
    const next = current.map((t) => (t.key === key ? { ...t, conversation: conversationId } : t));
    tabsRef.current = next;
    setTabs(next);
  }, []);

  const setTabParams = useCallback((key: string, params: TabParams): string | null => {
    const current = tabsRef.current;
    const tab = current.find((t) => t.key === key);
    if (!tab) return null;
    const clean: TabParams = { ...params };
    const nextKey = tabKeyFor(tab.screen, clean);
    let next: Tab[];
    if (nextKey === key) {
      next = current.map((t) => (t.key === key ? { ...t, params: clean } : t));
    } else if (current.some((t) => t.key === nextKey)) {
      next = current.filter((t) => t.key !== key); // the same params are open already: keep that one
    } else {
      next = current.map((t) => (t.key === key ? { ...t, key: nextKey, params: clean } : t));
    }
    tabsRef.current = next;
    setTabs(next);
    if (nextKey !== key) setActiveKey((active) => (active === key ? nextKey : active));
    return nextKey;
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebar((s) => ((s + 1) % 3) as SidebarState);
    setFocus(false);
  }, []);
  const toggleRightPanel = useCallback(() => {
    setRightPanel((v) => !v);
    setFocus(false);
  }, []);
  const toggleTerminal = useCallback(() => setTerminal((v) => !v), []);
  const toggleFocus = useCallback(() => setFocus((v) => !v), []);

  const setTheme = useCallback(
    (next: Theme) => {
      setThemeState(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        /* storage unavailable */
      }
      emit("theme:changed", { theme: next });
    },
    [emit],
  );

  const setLocale = useCallback((next: Locale) => {
    setI18nLocale(next);
    setLocaleState(next);
  }, []);

  const openSettings = useCallback((section?: string) => setSettings((s) => ({ open: true, section: section ?? s.section })), []);
  const closeSettings = useCallback(() => {
    setSettings((s) => ({ ...s, open: false }));
    emit("settings:closed");
  }, [emit]);

  const focusSearch = useCallback(() => {
    if (sidebarRef.current !== 0 || focusRef.current) {
      setSidebar(0);
      setFocus(false);
    }
    requestAnimationFrame(() => document.getElementById("sidebar-search")?.focus());
  }, []);

  const newConversation = useCallback(() => openScreen("chat", draftTab().params), [openScreen]);

  const dismissToast = useCallback((id: number) => setToasts((list) => list.filter((item) => item.id !== id)), []);
  const toast = useCallback(
    ({ tone = "info", text, durationMs = 5000 }: ToastInput) => {
      toastId.current += 1;
      const id = toastId.current;
      setToasts((list) => [...list.slice(-3), { id, tone, text }]);
      setTimeout(() => dismissToast(id), durationMs);
    },
    [dismissToast],
  );

  const value = useMemo<ShellApi>(
    () => ({
      tabs,
      activeKey,
      openScreen,
      closeTab,
      activateTab: setActiveKey,
      setTabTitle,
      bindConversation,
      setTabParams,
      layout: { sidebar, rightPanel, terminal, focus },
      toggleSidebar,
      toggleRightPanel,
      toggleTerminal,
      toggleFocus,
      theme,
      setTheme,
      locale,
      setLocale,
      settings,
      openSettings,
      closeSettings,
      sidebarQuery,
      setSidebarQuery,
      focusSearch,
      newConversation,
      toasts,
      toast,
      dismissToast,
      emit,
      on,
    }),
    [
      tabs, activeKey, openScreen, closeTab, setTabTitle, bindConversation, setTabParams, sidebar, rightPanel, terminal, focus, toggleSidebar, toggleRightPanel,
      toggleTerminal, toggleFocus, theme, setTheme, locale, setLocale, settings, openSettings, closeSettings, sidebarQuery,
      focusSearch, newConversation, toasts, toast, dismissToast, emit, on,
    ],
  );

  return <ShellContext.Provider value={value}>{children}</ShellContext.Provider>;
}

export function useShell(): ShellApi {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell must be used inside <ShellProvider>");
  return ctx;
}

/** Subscribes to a shell event for the lifetime of the component. */
export function useShellEvent(name: string, handler: (payload: unknown) => void): void {
  const { on } = useShell();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => on(name, (payload) => ref.current(payload)), [on, name]);
}
