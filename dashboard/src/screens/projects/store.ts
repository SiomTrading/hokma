import { ApiError } from "@/api";
import { t } from "@/i18n";
import * as client from "./api";
import type { ProjectList, ProjectTree } from "./types";

/**
 * State of the projects panel. It lives outside React so the "create" button in the panel header and the panel
 * body (two slots, two component trees) read the same data.
 */
export interface ProjectsSnapshot {
  list: ProjectList | null;
  listError: string | null;
  loading: boolean;
  tree: ProjectTree | null;
  treeError: string | null;
  treeLoading: boolean;
  dialog: "create" | "rename" | "link" | null;
  preview: { projectId: number; path: string; size: number } | null;
}

const EMPTY: ProjectsSnapshot = {
  list: null, listError: null, loading: false, tree: null, treeError: null, treeLoading: false, dialog: null, preview: null,
};

let snapshot: ProjectsSnapshot = EMPTY;
const listeners = new Set<() => void>();
let loadToken = 0;

function set(patch: Partial<ProjectsSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  listeners.forEach((fn) => fn());
}

export const subscribe = (fn: () => void): (() => void) => {
  listeners.add(fn);
  return () => listeners.delete(fn);
};
export const getSnapshot = (): ProjectsSnapshot => snapshot;

export function resetProjectsStore(): void {
  loadToken += 1;
  snapshot = EMPTY;
  listeners.forEach((fn) => fn());
}

export const messageOf = (cause: unknown): string => (cause instanceof ApiError ? cause.message : t("projects.error.generic"));

export function openDialog(dialog: ProjectsSnapshot["dialog"]): void {
  set({ dialog });
}

export function openPreview(preview: ProjectsSnapshot["preview"]): void {
  set({ preview });
}

async function loadTree(id: number | null, token: number): Promise<void> {
  if (id === null) {
    set({ tree: null, treeError: null, treeLoading: false });
    return;
  }
  set({ treeLoading: true, treeError: null });
  try {
    const tree = await client.projectTree(id);
    if (token === loadToken) set({ tree, treeLoading: false });
  } catch (cause) {
    if (token === loadToken) set({ tree: null, treeError: messageOf(cause), treeLoading: false });
  }
}

/** Reload the project list and the tree of the active project. Returns the list, or null when it failed. */
export async function refresh(): Promise<ProjectList | null> {
  loadToken += 1;
  const token = loadToken;
  set({ loading: true, listError: null });
  try {
    const list = await client.listProjects();
    if (token !== loadToken) return null;
    set({ list, loading: false });
    await loadTree(list.active_id, token);
    return list;
  } catch (cause) {
    if (token === loadToken) set({ listError: messageOf(cause), loading: false });
    return null;
  }
}

/** Reload only the tree (after an upload or a link). */
export async function refreshTree(): Promise<void> {
  await loadTree(snapshot.list?.active_id ?? null, loadToken);
}
