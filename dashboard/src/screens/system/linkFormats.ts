const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const GITHUB_URL = /^(?:https?:\/\/)?(?:www\.)?github\.com\/(.+)$/i;
const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const URL_PATTERN = /^https:\/\/\S+$/;

/** "owner/repo" from "owner/repo" or a GitHub link (https, with or without .git, trailing slash, extra path). Null if it is neither. */
export function normalizeRepo(raw: string): string | null {
  let text = raw.trim();
  const link = GITHUB_URL.exec(text);
  if (link) text = link[1].split(/[?#]/)[0];
  else if (/^[a-z]+:\/\//i.test(text) || text.includes("@")) return null;
  const parts = text.split("/").filter(Boolean);
  if (parts.length < 2 || (!link && parts.length !== 2)) return null;
  const joined = parts[0] + "/" + parts[1].replace(/\.git$/i, "");
  return REPO_PATTERN.test(joined) && !joined.endsWith("/") ? joined : null;
}

/** Why the repository field cannot be sent yet, or null when it can. */
export function repoProblem(raw: string): "empty" | "format" | null {
  if (!raw.trim()) return "empty";
  return normalizeRepo(raw) ? null : "format";
}

/** Lowercase, no accents, spaces to a hyphen, nothing else outside a-z 0-9 - _ , at most 32 characters. */
export function normalizeServerName(raw: string): string {
  return raw
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9_-]/g, "")
    .replace(/^[-_]+/, "")
    .slice(0, 32);
}

export type ServerProblem = "name_empty" | "name_reserved" | "name_taken" | "url_empty" | "url_format";

/** Why the add form cannot be sent yet, or null. `taken`: names already in the list (a duplicate says so). */
export function serverProblem(name: string, url: string, taken: readonly string[] = []): ServerProblem | null {
  const clean = name.trim().replace(/[-_]+$/, "");
  if (!clean || !NAME_PATTERN.test(clean)) return "name_empty";
  if (clean === "hokma") return "name_reserved";
  if (taken.includes(clean)) return "name_taken";
  return urlProblem(url);
}

/** Why an address cannot be sent yet, or null. */
export function urlProblem(url: string): "url_empty" | "url_format" | null {
  if (!url.trim()) return "url_empty";
  if (!URL_PATTERN.test(url.trim())) return "url_format";
  return null;
}
