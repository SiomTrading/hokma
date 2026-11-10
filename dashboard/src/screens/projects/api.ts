import { api } from "@/api";
import type { LinkResult, ProjectInfo, ProjectKind, ProjectList, ProjectTree, SaveResult, UploadResult } from "./types";

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

const base = (id: number) => `/api/projects/${id}`;

export const listProjects = (): Promise<ProjectList> => api.get<ProjectList>("/api/projects");

export const createProject = (name: string, kind: ProjectKind): Promise<ProjectInfo> =>
  api.post<ProjectInfo>("/api/projects", { name, kind });

export const renameProject = (id: number, name: string): Promise<ProjectInfo> => api.patch<ProjectInfo>(base(id), { name });

export const deactivateProject = (): Promise<ProjectList> => api.post<ProjectList>("/api/projects/deactivate");

export const activateProject = (id: number): Promise<ProjectList> => api.post<ProjectList>(`${base(id)}/activate`);

export const projectTree = (id: number): Promise<ProjectTree> => api.get<ProjectTree>(`${base(id)}/tree`);

export function uploadProjectFile(id: number, file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file, file.name || "file");
  return api.post<UploadResult>(`${base(id)}/files`, form, { timeoutMs: 180_000 });
}

export const addProjectLink = (id: number, url: string, title: string, download: boolean): Promise<LinkResult> =>
  api.post<LinkResult>(`${base(id)}/links`, { url, title, download }, { timeoutMs: 180_000 });

/** Copy chat attachments (by id) and short texts into the project. The chat panel can call this too. */
export const saveToProject = (
  id: number,
  items: { attachments?: string[]; notes?: { name: string; text: string }[] },
): Promise<SaveResult> => api.post<SaveResult>(`${base(id)}/save`, { attachments: items.attachments ?? [], notes: items.notes ?? [] });

export interface FilePreview {
  mime: string;
  /** Text of the file (first 200 KB), only for text types. */
  text?: string;
  /** Object URL for images, audio and anything else. Revoke it when done. */
  url?: string;
}

export const PREVIEW_TEXT_LIMIT = 200 * 1024;

export async function loadFile(id: number, path: string): Promise<FilePreview> {
  const query = `path=${encodeURIComponent(path)}`;
  const probe = await api.request(`${base(id)}/file?${query}&limit=1`);
  const mime = (probe.headers.get("content-type") ?? "").split(";")[0]!.trim();
  await probe.arrayBuffer();
  if (mime === "text/plain") {
    const text = await api.request(`${base(id)}/file?${query}&limit=${PREVIEW_TEXT_LIMIT}`);
    return { mime, text: await text.text() };
  }
  const full = await api.request(`${base(id)}/file?${query}`, { timeoutMs: 180_000 });
  return { mime, url: URL.createObjectURL(await full.blob()) };
}
