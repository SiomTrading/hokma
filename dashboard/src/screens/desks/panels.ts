/**
 * The panels a desk screen can show (SPEC-025, delivery 3). One list for the whole screen: every delivery that adds a panel (Analysis, Strategies,
 * Detail, Calendar, Candidates) adds one line here and one in `backend/hokma/desks/panels.py`.
 *
 * `rules`, `agents` and `config` are fixed: they can move but never leave. The rest can be added and taken off by hand or by the chief.
 * A desk with no saved layout shows DEFAULT_PANELS, the five panels every desk had before.
 */
export const DESK_PANELS = ["terminal", "agents", "operations", "rules", "config", "company", "calendar", "analysis", "strategies", "option_detail"] as const;
export type DeskTab = (typeof DESK_PANELS)[number];

/** Cannot be taken off the screen. */
export const FIXED_PANELS: readonly DeskTab[] = ["rules", "agents", "config"];
/** What a desk without layout shows, in this order. */
export const DEFAULT_PANELS: readonly DeskTab[] = ["terminal", "agents", "operations", "rules", "config"];

export const isDeskPanel = (value: unknown): value is DeskTab => typeof value === "string" && (DESK_PANELS as readonly string[]).includes(value);
export const isFixedPanel = (id: DeskTab): boolean => FIXED_PANELS.includes(id);

/** The order a desk shows: the one the server sent, cleaned of what this screen does not know, never without the fixed panels. */
export function panelsOf(desk: { panels?: readonly string[] | null }): DeskTab[] {
  if (!desk.panels) return [...DEFAULT_PANELS];
  const seen: DeskTab[] = [];
  for (const id of desk.panels) if (isDeskPanel(id) && !seen.includes(id)) seen.push(id);
  for (const id of FIXED_PANELS) if (!seen.includes(id)) seen.push(id);
  return seen;
}

/** Panels that are not on the screen, in catalog order: what the "Add" menu offers. The fixed ones are never missing. */
export const missingPanels = (order: readonly DeskTab[]): DeskTab[] => DESK_PANELS.filter((id) => !order.includes(id));

/** `id` moved `step` places (negative: left). The same order when it is already at that end. */
export function moveBy(order: readonly DeskTab[], id: DeskTab, step: number): DeskTab[] {
  const from = order.indexOf(id);
  const to = Math.min(order.length - 1, Math.max(0, from + step));
  if (from < 0 || to === from) return [...order];
  const next = order.filter((item) => item !== id);
  next.splice(to, 0, id);
  return next;
}

/** `id` dropped on `target`, on its left side (`before`) or its right side. */
export function dropAt(order: readonly DeskTab[], id: DeskTab, target: DeskTab, before: boolean): DeskTab[] {
  if (id === target || !order.includes(id) || !order.includes(target)) return [...order];
  const next = order.filter((item) => item !== id);
  next.splice(next.indexOf(target) + (before ? 0 : 1), 0, id);
  return next;
}

export const sameOrder = (a: readonly string[], b: readonly string[]): boolean => a.length === b.length && a.every((id, i) => id === b[i]);
