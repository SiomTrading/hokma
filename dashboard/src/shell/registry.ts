import type { ComponentType } from "react";
import type { IconName } from "@/design";
import type { Params } from "@/i18n";

export type TabParams = Record<string, string>;

export interface ScreenProps {
  /** Unique key of the tab showing this screen. */
  tabKey: string;
  params: TabParams;
  /** False only when the screen is registered with `keepAlive` and its tab is in the background. */
  active: boolean;
}

export type Translate = (key: string, params?: Params) => string;

export interface ScreenMeta {
  titleKey: string;
  icon: IconName;
  dot: "acc" | "org" | "muted";
  /** Show it in the "Modules" list of the "+" menu. */
  addMenu?: boolean;
}

export interface ScreenRegistration {
  id: string;
  component: ComponentType<ScreenProps>;
  keepAlive?: boolean;
  /** Tab title. Default is the built-in title of the screen id. */
  title?: (params: TabParams, t: Translate) => string;
  /** Required only for ids that are not built-in. */
  meta?: ScreenMeta;
}

const screens = new Map<string, ScreenRegistration>();

export function registerScreen(registration: ScreenRegistration): void {
  screens.set(registration.id, registration);
}

export function getScreen(id: string): ScreenRegistration | undefined {
  return screens.get(id);
}

/* ---------- Slots ---------- */

export type SlotName =
  | "sidebar.conversations"
  | "sidebar.desks"
  | "brand.subtitle"
  | "addMenu.assets"
  | "terminal"
  | "statusBar"
  | "statusBar.end"
  | "overlay.firstRun";

export interface SlotEntry {
  component: ComponentType<Record<string, unknown>>;
  order: number;
}

const slots = new Map<SlotName, SlotEntry[]>();

export function registerSlot<P extends object = Record<string, never>>(
  name: SlotName,
  component: ComponentType<P>,
  options: { order?: number } = {},
): void {
  const list = slots.get(name) ?? [];
  list.push({ component: component as unknown as SlotEntry["component"], order: options.order ?? 0 });
  list.sort((a, b) => a.order - b.order);
  slots.set(name, list);
}

export function getSlot(name: SlotName): readonly SlotEntry[] {
  return slots.get(name) ?? [];
}

/* ---------- Side panel sections ---------- */

export interface PanelSectionRegistration {
  id: string;
  /** Position in the column, top to bottom. */
  order: number;
  /** Takes the height that is left (the others take their own, with a shared cap). */
  fill?: boolean;
  titleKey: string;
  component: ComponentType;
  /** Buttons at the right of the title (the "+" of Projects). */
  actions?: ComponentType;
  /** Short text at the right of the title while the section is folded. A hook, so it can read live data. */
  useSummary?: () => string;
}

const panelSections = new Map<string, PanelSectionRegistration>();

export function registerPanelSection(registration: PanelSectionRegistration): void {
  panelSections.set(registration.id, registration);
}

export function getPanelSections(): readonly PanelSectionRegistration[] {
  return Array.from(panelSections.values()).sort((a, b) => a.order - b.order);
}

/* ---------- Settings sections ---------- */

export interface SettingsSectionRegistration {
  id: string;
  component: ComponentType;
  /** Required only for ids that are not built-in. */
  titleKey?: string;
  icon?: IconName;
  order?: number;
}

const sections = new Map<string, SettingsSectionRegistration>();

export function registerSettingsSection(registration: SettingsSectionRegistration): void {
  sections.set(registration.id, registration);
}

export function getSettingsSection(id: string): SettingsSectionRegistration | undefined {
  return sections.get(id);
}

export function getSettingsSections(): readonly SettingsSectionRegistration[] {
  return Array.from(sections.values());
}

/** Test helper: clears everything registered so far. */
export function resetRegistry(): void {
  screens.clear();
  slots.clear();
  sections.clear();
  panelSections.clear();
}
