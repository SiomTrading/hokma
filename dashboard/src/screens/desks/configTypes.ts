/** Contract of `GET /api/desks/{id}` (SPEC-014, section 3), reduced to what the Settings tab edits. */
export interface DeskDetail {
  id: number;
  name: string;
  focus: string | null;
  capital: number;
  objective_pct: number | null;
  mandate: string | null;
  token_budget: number | null;
  assets: string[];
}

/** Body of `PATCH /api/desks/{id}` for the Settings tab. */
export interface DeskPatch {
  name?: string;
  focus?: string | null;
  capital?: number;
  objective_pct?: number | null;
  mandate?: string | null;
  token_budget?: number | null;
}

/** One provider of `GET /api/desks/{id}/connections`: the global connection plus whether this desk uses it. */
export interface DeskConnection {
  provider_id: string;
  name: string;
  enabled: boolean;
  asset_classes: string[];
}

/** Sections of the Settings tab. The last two are empty states. */
export const CONFIG_SECTIONS = ["objective", "connections", "memory", "accounts"] as const;
export type ConfigSection = (typeof CONFIG_SECTIONS)[number];
