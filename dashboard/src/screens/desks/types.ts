import { DEFAULT_PANELS, isDeskPanel } from "./panels";
import type { DeskTab } from "./panels";

/** Contract of `GET /api/desks` (SPEC-014, section 3). */
export type DeskStatus = "active" | "paused" | "archived";
export type DeskTemplate = "minimal" | "standard" | "debate" | "custom";
export type BudgetState = "ok" | "warn" | "exhausted";

/**
 * One account of a desk (SPEC-027). `cash` and `equity` are null when the broker cannot tell: say so, never show 0.
 * `shared_with` lists the OTHER desks that hold the same account; `strategies` the strategies of this desk that trade through it.
 */
export interface DeskAccount {
  id: number;
  name: string;
  broker: string;
  /** Masked number (••••1234) of an account of a real broker, or null. */
  mask?: string | null;
  initial_capital: number;
  balance: { cash: number | null; equity: number | null; currency: string };
  as_of: string;
  shared_with?: number[];
  strategies?: string[];
}

export interface DeskSummary {
  id: number;
  project_id: number;
  name: string;
  focus: string | null;
  /** Null: a desk is born without money. Its accounts hold the money. */
  capital: number | null;
  currency: string;
  status: DeskStatus;
  template: DeskTemplate;
  /** Result of the day. Always 0 until there are trades. */
  today: number;
  /** Null until there are trades. */
  drawdown: number | null;
  agents_total: number;
  agents_working: number;
  tokens_week: number;
  /** Weekly cap of the desk. Null means no cap of its own. */
  token_budget: number | null;
  budget_state: BudgetState;
  /** The panels of the screen, in order (SPEC-025, delivery 3). Absent in old data: the five of before. */
  panels?: string[];
  /** The saved layout; null until the owner or the chief changes the panels. */
  layout?: { panels: string[] } | null;
  /** The accounts of the desk. Empty: the desk only analyzes. Optional for old data. */
  accounts?: DeskAccount[];
  /** strategy key -> account id, for the strategies that already trade through an account. */
  strategies?: Record<string, number>;
}

/** The accounts of a desk, empty when the server sent none. */
export const accountsOf = (desk: Pick<DeskSummary, "accounts">): DeskAccount[] => desk.accounts ?? [];

export interface DeskList {
  desks: DeskSummary[];
}

/** The five tabs every desk had before the layout (SPEC-025, delivery 3). The panels a desk shows now are `panelsOf(desk)`. */
export const DESK_TABS = DEFAULT_PANELS;
export type { DeskTab };

export function isDeskTab(value: string | undefined): value is DeskTab {
  return isDeskPanel(value);
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
