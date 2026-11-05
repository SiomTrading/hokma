import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ToastItem, ToastTone } from "@/design";
import { getLocale, setLocale as setI18nLocale } from "@/i18n";
import type { Locale } from "@/i18n";
import type { TabParams } from "./registry";

export type Theme = "dark" | "light";
export type SidebarState = 0 | 1 | 2; // 0 expanded, 1 icons only, 2 hidden

export interface Tab {
  key: string;
  screen: string;
  params: TabParams;
  /** Runtime override set by the screen (e.g. conversation name). */
  title?: string;
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
  openScreen: (screen: string, params?: TabParams) => void;
  closeTab: (key: string) => void;
  activateTab: (key: string) => void;
  setTabTitle: (key: string, title: string | undefined) => void;

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

const UI_KEY = "hokma.ui.v1";
const THEME_KEY = "hokma.theme";

export function tabKeyFor(screen: string, params: TabParams = {}): string {
  const entries = Object.entries(params).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return screen;
  return `${screen}:${entries.map(([k, v]) => `${k}=${v}`).join("&")}`;
}

const DEFAULT_TABS: Tab[] = [
  { key: "chat", screen: "chat", params: {} },
  { key: "panel", screen: "panel", params: {} },
];

interface Persisted {
  tabs: Tab[];
  activeKey: string | null;
  sidebar: SidebarState;
  rightPanel: boolean;
  terminal: boolean;
}

function isParams(value: unknown): value is TabParams {
  return !!value && typeof value === "object" && Object.values(value).every((v) => typeof v === "string");
}

function loadPersisted(): Persisted {
  const fallback: Persisted = { tabs: DEFAULT_TABS, activeKey: "chat", sidebar: 0, rightPanel: true, terminal: true };
  try {
    const raw = localStorage.getItem(UI_KEY);
    if (!raw) return fallback;
    const data = JSON.parse(raw) as Partial<Persisted>;
    const tabs = Array.isArray(data.tabs)
      ? data.tabs.filter((t): t is Tab => !!t && typeof t.screen === "string" && typeof t.key === "string" && isParams(t.params))
      : fallback.tabs;
    const activeKey = typeof data.activeKey === "string" && tabs.some((t) => t.key === data.activeKey) ? data.activeKey : (tabs[tabs.length - 1]?.key ?? null);
    return {
      tabs,
      activeKey,
      sidebar: data.sidebar === 1 || data.sidebar === 2 ? data.sidebar : 0,
      rightPanel: typeof data.rightPanel === "boolean" ? data.rightPanel : true,
      terminal: typeof data.terminal === "boolean" ? data.terminal : true,
    };
  } catch {
    return fallback;
  }
}

function loadTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

const ShellContext = createContext<ShellApi | null>(null);

export function ShellProvider({ children }: { children: ReactNode }) {
  const initial = useRef<Persisted | null>(null);
  if (initial.current === null) initial.current = loadPersisted();
  const start = initial.current;

  const [tabs, setTabs] = useState<Tab[]>(start.tabs);
  const [activeKey, setActiveKey] = useState<string | null>(start.activeKey);
  const [sidebar, setSidebar] = useState<SidebarState>(start.sidebar);
  const [rightPanel, setRightPanel] = useState(start.rightPanel);
  const [terminal, setTerminal] = useState(start.terminal);
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

  useEffect(() => {
    try {
      localStorage.setItem(UI_KEY, JSON.stringify({ tabs, activeKey, sidebar, rightPanel, terminal } satisfies Persisted));
    } catch {
      /* storage unavailable */
    }
  }, [tabs, activeKey, sidebar, rightPanel, terminal]);

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
    const key = tabKeyFor(screen, params);
    setTabs((current) => (current.some((t) => t.key === key) ? current : [...current, { key, screen, params }]));
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

  const newConversation = useCallback(() => {
    openScreen("chat");
    emit("conversation:new");
  }, [openScreen, emit]);

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
      tabs, activeKey, openScreen, closeTab, setTabTitle, sidebar, rightPanel, terminal, focus, toggleSidebar, toggleRightPanel,
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
