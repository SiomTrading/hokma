export type ClaudeStatus = "connected" | "no_session" | "no_cli" | "unknown";

export interface WorkdirOption {
  id: "documents" | "disk" | "sync";
  provider: string | null;
  path: string;
}

export interface ProfileDto {
  owner_name: string;
  fund_name: string | null;
  language: "es" | "en";
  theme: "dark" | "light";
  workdir: string | null;
  database_engine: "sqlite" | "postgres";
  claude_status: ClaudeStatus;
  claude_plan: string | null;
  setup_completed_at: string | null;
  permission_mode?: "ask" | "auto";
  permission_mode_intro_seen?: boolean;
}

export interface SetupState {
  completed: boolean;
  profile: ProfileDto | null;
  owner: { owner_name: string; fund_name: string };
  workdir: { path: string; exists: boolean; git: boolean; manual: boolean; options: WorkdirOption[] };
  database: { engine: "sqlite" | "postgres"; location: string | null };
  agents: { total: number };
  claude: { state: ClaudeStatus; plan: string | null };
  version: string;
}

export interface LoginInfo {
  running: boolean;
  url: string | null;
  error?: string;
}

export interface ClaudeDto {
  state: "connected" | "no_session" | "no_cli";
  installed: boolean;
  plan: string | null;
  version: string | null;
  login: LoginInfo;
}

export interface DirEntry {
  name: string;
  path: string;
}

export interface DirRoot extends DirEntry {
  kind: "home" | "drive";
}

export interface DirListing {
  path: string | null;
  parent: string | null;
  dirs: DirEntry[];
  roots: DirRoot[];
}

export interface WorkdirResult {
  path: string;
  created: boolean;
  git_initialized: boolean;
  git: boolean;
}

export type DbChoice = "sqlite" | "postgres";

export interface MigrationDto {
  ok: boolean;
  engine: DbChoice;
  location: string | null;
  rows: number;
  counts: Record<string, number>;
  schema_version: number;
  notes: string[];
  message: string;
}
