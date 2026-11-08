/** Wire types of the permission mode, the always-on protections, the trash and the restore points. */

export type PermissionMode = "ask" | "auto";

export const RULES = ["delete", "truncate", "hokma_internal", "force_push", "outside_workdir", "secrets", "real_money"] as const;
export type RuleId = (typeof RULES)[number];

export function isRule(value: unknown): value is RuleId {
  return typeof value === "string" && (RULES as readonly string[]).includes(value);
}

export function isMode(value: unknown): value is PermissionMode {
  return value === "ask" || value === "auto";
}

export interface ModeState {
  mode: PermissionMode;
  intro_seen: boolean;
}

export interface ModeChange extends ModeState {
  changed: boolean;
  turns_running: number;
}

export interface Protection {
  id: string;
  title: string;
}

export interface ProtectionList {
  always_on: boolean;
  rules: Protection[];
}

export interface TrashItem {
  id: number;
  name: string;
  original_path: string;
  project_id: number | null;
  project_name: string | null;
  conversation_id: number | null;
  conversation_title: string | null;
  size_bytes: number;
  moved_at: string;
  reason: string;
  is_dir: boolean;
  present: boolean;
}

export interface TrashList {
  items: TrashItem[];
  count: number;
  total_bytes: number;
}

export interface RestorePoint {
  id: string;
  at: string;
  conversation_id: string | null;
}

export interface RestorePointList {
  project: { id: string | number; name: string };
  points: RestorePoint[];
}

export interface PointChanges {
  id: string;
  deleted: string[];
  changed: string[];
  truncated: boolean;
}

export interface RestoreResult {
  restored: string[];
  renamed?: unknown;
  count?: number;
}
