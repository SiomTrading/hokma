import type { IconName } from "@/design";
import { getScreen, getSettingsSections } from "./registry";
import type { ScreenMeta } from "./registry";

/** Screens that already have a place in the sidebar, the "+" menu and the tab strip. */
export const BUILTIN_SCREENS: Record<string, ScreenMeta & { navKey: string; menuKey?: string }> = {
  chat: { titleKey: "screen.chat", navKey: "nav.chat", menuKey: "nav.newChat", icon: "chat", dot: "acc", addMenu: true },
  panel: { titleKey: "screen.panel", navKey: "nav.panel", menuKey: "nav.panel", icon: "panel", dot: "muted", addMenu: true },
  chart: { titleKey: "screen.chart", navKey: "nav.chart", icon: "chart", dot: "org" },
  library: { titleKey: "screen.library", navKey: "nav.library", icon: "library", dot: "muted" },
  connections: { titleKey: "screen.connections", navKey: "nav.connections", icon: "connections", dot: "muted" },
  tasks: { titleKey: "screen.tasks", navKey: "nav.tasks", menuKey: "nav.tasks", icon: "tasks", dot: "muted", addMenu: true },
  learn: { titleKey: "screen.learn", navKey: "nav.learn", menuKey: "nav.learn", icon: "learn", dot: "muted", addMenu: true },
};

/** Order of the main sidebar entries, as in the design. */
export const SIDEBAR_SCREENS = ["chat", "panel", "chart", "library", "connections", "tasks"] as const;
/** Order of the "Modules" list in the "+" menu, as in the design. */
export const ADD_MENU_SCREENS = ["chat", "panel", "tasks", "learn"] as const;

export function screenMeta(id: string): ScreenMeta | undefined {
  return BUILTIN_SCREENS[id] ?? getScreen(id)?.meta;
}

export const DOT_COLOR: Record<ScreenMeta["dot"], string> = {
  acc: "var(--acc)",
  org: "var(--org)",
  muted: "var(--t3)",
};

export interface SettingsSectionMeta {
  id: string;
  titleKey: string;
  icon: IconName;
  order: number;
}

const BUILTIN_SECTIONS: SettingsSectionMeta[] = [
  { id: "general", titleKey: "settings.general", icon: "general", order: 0 },
  { id: "agents", titleKey: "settings.agents", icon: "agents", order: 1 },
  { id: "models", titleKey: "settings.models", icon: "models", order: 2 },
  { id: "extensions", titleKey: "settings.extensions", icon: "extensions", order: 3 },
  { id: "status", titleKey: "settings.status", icon: "status", order: 4 },
  { id: "news", titleKey: "settings.news", icon: "news", order: 5 },
];

export function settingsSectionList(): SettingsSectionMeta[] {
  const list = [...BUILTIN_SECTIONS];
  for (const reg of getSettingsSections()) {
    if (list.some((s) => s.id === reg.id)) continue;
    if (!reg.titleKey || !reg.icon) continue;
    list.push({ id: reg.id, titleKey: reg.titleKey, icon: reg.icon, order: reg.order ?? 100 });
  }
  return list.sort((a, b) => a.order - b.order);
}
