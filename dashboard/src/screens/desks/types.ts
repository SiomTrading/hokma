/** Contract of `GET /api/desks` (SPEC-014, section 3). */
export type DeskStatus = "active" | "paused" | "archived";
export type DeskTemplate = "minimal" | "standard" | "debate" | "custom";
export type BudgetState = "ok" | "warn" | "exhausted";

export interface DeskSummary {
  id: number;
  project_id: number;
  name: string;
  focus: string | null;
  capital: number;
  currency: string;
  status: DeskStatus;
  template: DeskTemplate;
  /** Result of the day. Always 0 until simulated operations exist. */
  today: number;
  /** Null until simulated operations exist. */
  drawdown: number | null;
  agents_total: number;
  agents_working: number;
  tokens_week: number;
  /** Weekly cap of the desk. Null means no cap of its own. */
  token_budget: number | null;
  budget_state: BudgetState;
}

export interface DeskList {
  desks: DeskSummary[];
}

/** The five tabs of a desk, in the order they are shown. */
export const DESK_TABS = ["terminal", "agents", "operations", "rules", "config"] as const;
export type DeskTab = (typeof DESK_TABS)[number];

export function isDeskTab(value: string | undefined): value is DeskTab {
  return DESK_TABS.includes(value as DeskTab);
}

export type DeskModel = "haiku" | "sonnet" | "opus";

/** Roles an agent can have in a desk (`desk_agents.role`). */
export type DeskRole =
  | "chief" | "data" | "risk" | "research" | "signals" | "execution" | "reviewer"
  | "bull" | "bear" | "judge" | "strategy_dev" | "custom";

/** One member of a template: which agent, in which role, with which default model. */
export interface TemplateAgent {
  slug: string;
  role: DeskRole;
  model: DeskModel;
}

/** Templates the wizard offers. `custom` only exists for desks that were changed after creation. */
export type WizardTemplate = Exclude<DeskTemplate, "custom">;

/** Contract of `GET /api/desks/templates`. */
export interface DeskTemplateDef {
  id: WizardTemplate;
  agents: TemplateAgent[];
  /** Suggested weekly token budget. Null means no cap. */
  token_budget: number | null;
}

/** Body of `POST /api/desks`. */
export interface CreateDeskPayload {
  name: string;
  focus: string | null;
  capital: number;
  objective_pct?: number;
  mandate?: string;
  template: WizardTemplate;
  assets: string[];
  token_budget?: number;
}

/** Limits of the desk fields, the same ones the database enforces. */
export const DESK_LIMITS = {
  nameMax: 80,
  focusMax: 120,
  mandateMax: 4000,
  capitalMin: 100,
  capitalMax: 1_000_000_000,
  assetsMax: 20,
  symbolMax: 40,
} as const;
