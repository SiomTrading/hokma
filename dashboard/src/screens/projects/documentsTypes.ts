export type DocumentStatus = "indexing" | "ready" | "failed";

/** One document of the project library, as returned by `/api/projects/{id}/documents`. */
export interface DocumentInfo {
  id: number;
  name: string;
  format: string;
  size_bytes: number;
  status: DocumentStatus;
  status_detail: string | null;
  chunk_count: number;
  /** Set when the document came from a link; null for an uploaded file. */
  source_url: string | null;
  description: string | null;
}

export const IMAGE_FORMATS: ReadonlySet<string> = new Set(["png", "jpg", "jpeg", "gif", "webp"]);

export const MAX_DOCUMENT_BYTES = 50 * 1024 * 1024;

export function extensionOf(name: string): string {
  return name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
}

export const isImageName = (name: string): boolean => IMAGE_FORMATS.has(extensionOf(name));

/** One candidate listed by `discover`. */
export interface DiscoveredItem {
  url: string;
  title: string;
  kind: "pdf" | "html";
  host: string;
}

export interface DiscoverResult {
  items: DiscoveredItem[];
  /** True when the pasted address is itself a document. */
  page_is_document: boolean;
}

/** Why a link download failed, as named by the backend. */
export type FetchReason = "blocked" | "too_large" | "not_a_document" | "timeout" | "network" | "duplicate";

export const FETCH_REASONS: ReadonlySet<string> = new Set(["blocked", "too_large", "not_a_document", "timeout", "network", "duplicate"]);

/** At most this many candidates are listed, and at most this many can be marked per batch. */
export const MAX_LISTED_LINKS = 200;
export const MAX_MARKED_LINKS = 50;
