import { api } from "@/api";
import { isDeskPanel } from "./panels";
import type { DeskTab } from "./panels";

const base = (id: number) => `/api/desks/${id}/panels`;

/** The answer of every call is the desk; what the screen reads of it is its panels, in order. */
export const readPanels = (body: unknown): DeskTab[] | null => {
  const list = (body as { panels?: unknown } | null)?.panels;
  return Array.isArray(list) ? list.filter(isDeskPanel) : null;
};

/** Panels of a desk screen: the whole order after a move, one panel added, one panel taken off. */
export const deskPanelsApi = {
  set: (id: number, panels: readonly DeskTab[]) => api.put<unknown>(base(id), { panels }),
  add: (id: number, panel: DeskTab) => api.post<unknown>(base(id), { id: panel }),
  remove: (id: number, panel: DeskTab) => api.delete<unknown>(`${base(id)}/${panel}`),
};
