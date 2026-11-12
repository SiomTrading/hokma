import { api } from "@/api";

export type FolderStatus = "scanning" | "ready" | "missing" | "failed";

/** A disk folder a project reads. `own` marks the project's own folder, which is always there. */
export interface DiskFolder {
  id: number;
  project_id: number;
  path: string;
  own: boolean;
  label: string | null;
  status: FolderStatus;
  status_detail: string | null;
  file_count: number;
  last_scan_at: string | null;
}

export interface PickResult {
  available: boolean;
  path: string | null;
  cancelled: boolean;
}

const base = (projectId: number) => `/api/projects/${projectId}/folders`;

export async function listFolders(projectId: number): Promise<{ folders: DiskFolder[]; max: number }> {
  const data = await api.get<{ folders?: DiskFolder[]; max_folders?: number }>(base(projectId));
  return { folders: data?.folders ?? [], max: data?.max_folders ?? 5 };
}

export const addFolder = (projectId: number, path: string): Promise<DiskFolder> =>
  api.post<DiskFolder>(base(projectId), { path });

export const removeFolder = (projectId: number, folderId: number): Promise<void> =>
  api.delete<void>(`${base(projectId)}/${folderId}`);

/** Reads the folder again now; the answer is the folder with its new counts. */
export const rescanFolder = (projectId: number, folderId: number): Promise<DiskFolder> =>
  api.post<DiskFolder>(`${base(projectId)}/${folderId}/rescan`, {}, { timeoutMs: 300_000 });

/** Opens the system's own folder dialog. */
export const pickFolder = (): Promise<PickResult> =>
  api.post<PickResult>("/api/system/pick-folder", {}, { timeoutMs: 0 });
