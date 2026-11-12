import { api, ApiError } from "@/api";
import { FETCH_REASONS } from "./documentsTypes";
import type { DiscoveredItem, DiscoverResult, DocumentInfo, FetchReason } from "./documentsTypes";

const base = (id: number) => `/api/projects/${id}/documents`;

/** The list may come as a bare array or wrapped in `{ documents }`; both are accepted. */
export async function listDocuments(projectId: number): Promise<DocumentInfo[]> {
  const data = await api.get<DocumentInfo[] | { documents: DocumentInfo[] }>(base(projectId));
  return Array.isArray(data) ? data : (data?.documents ?? []);
}

/** Uploads one file. The backend indexes it inside the same request. */
export function uploadDocument(projectId: number, file: File, description?: string): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file, file.name || "file");
  if (description && description.trim()) form.append("description", description.trim());
  return api.post<DocumentInfo>(base(projectId), form, { timeoutMs: 240_000 });
}

export const deleteDocument = (projectId: number, documentId: number): Promise<void> =>
  api.delete<void>(`${base(projectId)}/${documentId}`);

/** Lists the documents found at an address. */
export async function discoverDocuments(projectId: number, url: string): Promise<DiscoverResult> {
  const data = await api.post<Partial<DiscoverResult>>(`${base(projectId)}/discover`, { url }, { timeoutMs: 60_000 });
  return { items: data?.items ?? [], page_is_document: Boolean(data?.page_is_document) };
}

/** Failure of a link download, with the backend's `reason` when it named one. */
export class FetchFailure extends Error {
  readonly reason: FetchReason | null;

  constructor(reason: FetchReason | null) {
    super(reason ?? "fetch_failed");
    this.name = "FetchFailure";
    this.reason = reason;
  }
}

/**
 * The backend sends `reason` next to the error code (`{detail: {code, message, reason}}`). Without it the status
 * tells: 409 (duplicate), 413 (too large) and a dead connection.
 */
function reasonOfError(cause: unknown): FetchReason | null {
  if (cause instanceof ApiError) {
    if (FETCH_REASONS.has(cause.reason)) return cause.reason as FetchReason;
    if (FETCH_REASONS.has(cause.code)) return cause.code as FetchReason;
    if (cause.status === 409) return "duplicate";
    if (cause.status === 413) return "too_large";
    if (cause.status === 0) return "network";
  }
  return null;
}

/** Downloads one candidate and indexes it. Throws {@link FetchFailure}. */
export async function fetchDocument(projectId: number, item: Pick<DiscoveredItem, "url" | "title">): Promise<DocumentInfo> {
  let data: (DocumentInfo & { reason?: string }) | undefined;
  try {
    data = await api.post<DocumentInfo & { reason?: string }>(
      `${base(projectId)}/fetch`,
      { url: item.url, title: item.title || undefined },
      { timeoutMs: 240_000 },
    );
  } catch (cause) {
    throw new FetchFailure(reasonOfError(cause));
  }
  // A 2xx body that carries a `reason` is a failure reported inside the answer.
  if (data?.reason) throw new FetchFailure(FETCH_REASONS.has(data.reason) ? (data.reason as FetchReason) : null);
  return data as DocumentInfo;
}
