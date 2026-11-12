import { api, ApiError } from "@/api";

/** One Library folder, as returned by `/api/library/folders`. */
export interface LibraryFolder {
  id: number;
  parent_id: number | null;
  name: string;
}

/** Contents of a folder that blocked its removal until the student confirms. */
export interface FolderContents {
  documents: number;
  subfolders: number;
}

/** The list may come as a bare array or wrapped in `{ folders }`; both are accepted. */
export async function listFolders(parentId: number | null): Promise<LibraryFolder[]> {
  const query = parentId == null ? "" : `?parent_id=${parentId}`;
  const data = await api.get<LibraryFolder[] | { folders: LibraryFolder[] }>(`/api/library/folders${query}`);
  return Array.isArray(data) ? data : (data?.folders ?? []);
}

export const createFolder = (parentId: number | null, name: string): Promise<LibraryFolder> =>
  api.post<LibraryFolder>("/api/library/folders", { parent_id: parentId, name });

export const renameFolder = (id: number, name: string): Promise<LibraryFolder> =>
  api.patch<LibraryFolder>(`/api/library/folders/${id}`, { name });

/** Result of asking to remove a folder: gone, or it has contents the student must confirm. */
export type RemoveResult = { removed: true } | { removed: false; contents: FolderContents };

/** Removes a folder. Without `confirm`, a folder with contents answers 409 with its counts instead. */
export async function removeFolder(id: number, confirm = false): Promise<RemoveResult> {
  try {
    await api.delete<void>(`/api/library/folders/${id}${confirm ? "?confirm=true" : ""}`);
    return { removed: true };
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 409 && !confirm) {
      return {
        removed: false,
        contents: { documents: Number(cause.data.documents ?? 0), subfolders: Number(cause.data.subfolders ?? 0) },
      };
    }
    throw cause;
  }
}
