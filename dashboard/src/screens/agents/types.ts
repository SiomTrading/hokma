export type ModelAlias = "haiku" | "sonnet" | "opus";

export interface AgentItem {
  slug: string;
  name: string;
  description: string;
  tier: "fast" | "balanced" | "deep";
  model: ModelAlias;
  status: "active" | "off";
  active: boolean;
  is_custom: boolean;
  is_orchestrator: boolean;
  enabled_session: number | null;
  role: string | null;
}

export interface AgentsData {
  orchestrator: AgentItem | null;
  agents: AgentItem[];
  active: number;
  total: number;
}

export interface NewAgent {
  name: string;
  description: string;
  model: ModelAlias;
  instructions: string;
}

export interface UsageRow {
  key: string;
  tokens_in: number;
  tokens_out: number;
}

export interface UsageData {
  starts_at: string;
  ends_at: string;
  used: number;
  tokens_in: number;
  tokens_out: number;
  messages: number;
  runs: number;
  cap: number | null;
  pct: number | null;
  by_agent: UsageRow[];
  by_model: UsageRow[];
  by_day: UsageRow[];
  claude: { status: "connected" | "no_session" | "no_cli" | "unknown"; plan: string | null; checked_at: string | null };
  profile_exists: boolean;
}

export type JobKind = "once" | "daily" | "weekly" | "interval";

export interface JobLast {
  task_id: number;
  status: string;
  summary: string;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  tokens: number;
}

export interface JobItem {
  id: number;
  name: string;
  agent: string;
  agent_name: string;
  agent_active: boolean;
  model: ModelAlias | null;
  prompt: string;
  kind: JobKind;
  expr: string;
  timezone: string;
  active: boolean;
  approved: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last: JobLast | null;
}

export interface JobsData {
  jobs: JobItem[];
  running: number[];
}

export interface JobInput {
  name: string;
  agent: string;
  prompt: string;
  kind: JobKind;
  expr: string;
  timezone: string;
}
