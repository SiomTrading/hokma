export type ProjectKind = "ips" | "wealth" | "strategy" | "research" | "robot" | "other";

export const PROJECT_KINDS: readonly ProjectKind[] = ["ips", "wealth", "strategy", "research", "robot", "other"];

export interface GitState {
  ok: boolean;
  /** "", "no_repo", "unsafe", "failed" or "missing". */
  reason: string;
}

export interface ProjectInfo {
  id: number;
  name: string;
  slug: string;
  kind: ProjectKind;
  path: string;
  active: boolean;
  exists: boolean;
  files: number;
  git: GitState;
}

export interface ProjectList {
  workdir: string;
  root: string;
  active_id: number | null;
  kinds: ProjectKind[];
  projects: ProjectInfo[];
}

export interface TreeEntry {
  path: string;
  name: string;
  type: "dir" | "file";
  depth: number;
  size?: number;
  duration?: number | null;
}

export interface ProjectTree {
  id: number;
  name: string;
  path: string;
  entries: TreeEntry[];
  truncated: boolean;
  git: GitState;
  commits: string[];
}

export interface UploadResult {
  path: string;
  size: number;
  created: boolean;
  git: GitState;
}

export interface LinkResult {
  link: string;
  file: string | null;
  downloaded: boolean;
  git: GitState;
}

export interface SaveResult {
  saved: string[];
  git: GitState;
}
