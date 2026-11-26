import { api } from "@/api";

export type ControlId =
  | "size_premium" | "size_contracts" | "size_margin" | "size_weight" | "position_value" | "daily_loss" | "drawdown" | "cooldown" | "events"
  | "daily_loss_pct" | "drawdown_pct" | "capital_floor" | "consistency" | "assets_only";
export type RuleSource = "chat" | "hand" | "suggested" | "existing";
export type RuleUnit = "usd" | "pct" | "contracts" | "minutes" | "assets";
/** SPEC-026: a restriction blocks; a notice only asks the owner and the order waits for his answer. */
export type RuleMode = "restriction" | "notice";
/** What happens when a loss limit is reached: only block entries, also close the open positions, or also sell what is held long. */
export type OnTrip = "block_entries" | "block_and_flatten" | "block_and_flatten_all";
/** `desk`: every account of the desk; `account`: the whole account (all the desks that use it); `desk_account`: this desk in one account. */
export type RuleScope = "desk" | "account" | "desk_account";
export interface ModeAnswers { mode: RuleMode; on_trip?: OnTrip; timeout_min?: number }

/** One rule of a desk: a control the guards apply or an instruction the chief reads. */
export interface Rule {
  id: string;
  kind: "control" | "instruction";
  control_id?: ControlId;
  value?: number | boolean;
  unit?: RuleUnit;
  /** Word of the event name, only for the "events" control. */
  match?: string | null;
  text?: string;
  source: RuleSource;
  /** False: the value is saved but its guard type is not in the list of the desk, so the code does not apply it. */
  on: boolean;
  readonly?: boolean;
  /** A rule saved before SPEC-026 has no mode of its own and is a restriction. */
  mode?: RuleMode;
  on_trip?: OnTrip;
  timeout_min?: number;
  scope?: RuleScope;
  account_id?: number;
  desk_id?: number | null;
}

/** An account of the desk and whether a loss rule closed it for the day. */
export interface AccountState {
  account_id: number;
  name: string;
  state: "open" | "day_closed";
  closed_at?: string;
  rule_id?: string;
  loss?: string | null;
  on_trip?: OnTrip | null;
}

export interface Suggestion {
  id: string;
  kind: "control" | "instruction";
  control_id?: ControlId;
  value?: number;
  unit?: RuleUnit;
  match?: string | null;
}

/** What the chief proposed and waits for the owner. */
export interface Proposal {
  id: string;
  kind: "control" | "instruction";
  control_id?: ControlId;
  /** Control proposals: the value, unit and word of the event come from what the server SAVED, never from `text`. */
  value?: number;
  unit?: RuleUnit | null;
  match?: string | null;
  /** The value the desk has now for that control (null when it has none). */
  current?: number | null;
  /** True: approving it raises or takes off a control the desk already has, so it needs a second, explicit confirmation. */
  loosens?: boolean;
  text: string;
  mode?: RuleMode;
  on_trip?: OnTrip;
  timeout_min?: number;
  account_id?: number;
  only_desk?: boolean;
}

export interface CatalogRow {
  id: ControlId;
  unit: RuleUnit;
  lines: string[] | null;
}

/** `GET /api/desks/{id}/rules`. `legacy`: the desk existed before the rules tab and runs every guard. */
export interface RulesView {
  desk_id: number;
  line: string | null;
  legacy: boolean;
  fixed: string[];
  catalog: CatalogRow[];
  active: Rule[];
  suggested: Suggestion[];
  proposed: Proposal[];
  assets: string[];
  assets_on: boolean;
  accounts?: AccountState[];
  account_rules?: Rule[];
}

export interface NewRule {
  kind: "control" | "instruction";
  control_id?: ControlId;
  value?: number;
  match?: string;
  text?: string;
  source: "hand" | "suggested";
  suggested_id?: string;
  mode?: RuleMode;
  on_trip?: OnTrip;
  timeout_min?: number;
  scope?: RuleScope;
  account_id?: number;
}

const base = (deskId: number) => `/api/desks/${deskId}/rules`;

export const rulesApi = {
  get: (deskId: number) => api.get<RulesView>(base(deskId)),
  add: (deskId: number, rule: NewRule) => api.post<RulesView>(base(deskId), rule),
  editControl: (deskId: number, ruleId: string, value: number, answers?: ModeAnswers) =>
    api.put<RulesView>(`${base(deskId)}/${encodeURIComponent(ruleId)}`, { value, ...answers }),
  /** Only the mode changes (also for the list of assets, whose list is edited in Configuration). */
  editMode: (deskId: number, ruleId: string, answers: ModeAnswers) => api.put<RulesView>(`${base(deskId)}/${encodeURIComponent(ruleId)}`, { ...answers }),
  /** The owner opens an account that a loss rule closed for the day. Without `confirm` the server answers 409 with what closed it. */
  liftDayClose: (accountId: number, confirm: boolean) => api.post<unknown>(`/api/accounts/${accountId}/day-close/lift`, { confirm }),
  editText: (deskId: number, ruleId: string, text: string) => api.put<RulesView>(`${base(deskId)}/${encodeURIComponent(ruleId)}`, { text }),
  remove: (deskId: number, ruleId: string) => api.delete<RulesView>(`${base(deskId)}/${encodeURIComponent(ruleId)}`),
  dismiss: (deskId: number, suggestedId: string) => api.post<RulesView>(`/api/desks/${deskId}/rules-dismiss`, { suggested_id: suggestedId }),
  approve: (deskId: number, proposalId: string, confirmLoosen = false) =>
    api.post<RulesView>(`/api/desks/${deskId}/rules-proposals/${proposalId}/approve`, confirmLoosen ? { confirm_loosen: true } : {}),
  reject: (deskId: number, proposalId: string) => api.post<RulesView>(`/api/desks/${deskId}/rules-proposals/${proposalId}/reject`, {}),
};
