import { api, ApiError } from "@/api";
import { t } from "@/i18n";
import type { ModeChange, ModeState, PermissionMode, PointChanges, ProtectionList, RestorePointList, RestoreResult, TrashList } from "./types";

const ERROR_CODES = new Set(["bad_mode", "confirm_required", "no_git", "bad_path", "unknown_point", "not_found", "restore_failed", "protected"]);

/** Translated text for an error of these endpoints: the catalog first, the server text when the code is not known. */
export function permissionErrorText(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (ERROR_CODES.has(cause.code)) return t(`permissions.err.${cause.code}`);
    return cause.message || t("permissions.err.generic");
  }
  return t("permissions.err.generic");
}

export function errorCode(cause: unknown): string | null {
  return cause instanceof ApiError ? cause.code : null;
}

const enc = encodeURIComponent;
const projectQuery = (projectId: string | null) => (projectId ? `?project_id=${enc(projectId)}` : "");

export const permissionsApi = {
  mode: () => api.get<ModeState>("/api/profile/permission-mode"),
  setMode: (body: { mode?: PermissionMode; intro_seen?: boolean }) => api.put<ModeChange>("/api/profile/permission-mode", body),
  protections: () => api.get<ProtectionList>("/api/protections"),

  trash: () => api.get<TrashList>("/api/trash"),
  restore: (id: number) => api.post<{ id: number; restored_to: string; renamed: boolean }>(`/api/trash/${id}/restore`),
  purge: (id: number) => api.post<{ id: number }>(`/api/trash/${id}/purge`, { confirm: true }),
  empty: () => api.post<{ count: number }>("/api/trash/empty", { confirm: true }),

  points: (projectId: string | null) => api.get<RestorePointList>(`/api/checkpoints${projectQuery(projectId)}`),
  changes: (point: string, projectId: string | null) => api.get<PointChanges>(`/api/checkpoints/${enc(point)}/changes${projectQuery(projectId)}`),
  restoreFile: (point: string, path: string, projectId: string | null) =>
    api.post<RestoreResult>(`/api/checkpoints/${enc(point)}/restore-file`, { path, project_id: projectId ?? undefined }),
  restoreDeleted: (point: string, projectId: string | null) =>
    api.post<RestoreResult>(`/api/checkpoints/${enc(point)}/restore-deleted`, { project_id: projectId ?? undefined }),
};
