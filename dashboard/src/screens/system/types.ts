/** Shapes of /api/extensions, /api/extensions/marketplace, /api/status, /api/news and /api/update. */

export interface PluginItem {
  id: string;
  name: string;
  marketplace: string;
  kind: "plugin";
  enabled: boolean;
  /** What turning it on does, as the CLI shows it. */
  summary: string;
}

export interface ServerItem {
  id: string;
  name: string;
  kind: "mcp";
  enabled: boolean;
  target: string;
  remote?: boolean;
}

export type RemoteState = "needs_auth" | "connected" | "error" | "no_cli";

export interface RemoteServer {
  name: string;
  url: string;
  state: RemoteState;
  message: string;
}

export interface MarketplaceSource {
  name: string;
  source: string;
}

export interface ExtensionRequest {
  id: string;
  origin: "inbox" | "db";
  kind: "plugin" | "mcp" | "skill";
  name: string;
  requested_at: string;
  reason: string | null;
  risks: string[];
  summary: string;
}

export interface ExtensionsData {
  plugins: PluginItem[];
  servers: ServerItem[];
  marketplaces: MarketplaceSource[];
  requests: ExtensionRequest[];
  problems: string[];
  database: boolean;
}

export interface MarketItem {
  id: string;
  name: string;
  display_name: string;
  description: string;
  category: string;
  installed: boolean;
  enabled: boolean;
}

export interface MarketData {
  marketplace: string;
  repo: string;
  added: boolean;
  total: number;
  items: MarketItem[];
}

export type CheckStatus = "ok" | "warn" | "fail";

export interface StatusCheck {
  name: string;
  status: CheckStatus;
  detail: string;
}

export interface StatusData {
  version: string;
  workdir: string;
  checks: StatusCheck[];
  summary: Record<CheckStatus, number>;
}

export interface ChangelogSection {
  version: string;
  body: string;
}

export interface NewsData {
  /** When the last successful search for a new version finished (ISO, UTC), or null if none yet. */
  checked_at: string | null;
  installed: { version: string; installed_at: string | null };
  changelog: ChangelogSection[];
  latest: { version: string; notes: string | null; published_at: string | null } | null;
  latest_error: string | null;
  newer: boolean;
}

export interface UpdateResult {
  /** "launched": the detached updater runs and Hokma restarts by itself (task 3146). */
  status: "launched" | "updated" | "up-to-date";
  from_version: string;
  to_version: string;
  restart_required: boolean;
  /** True when Hokma restarts by itself (it runs under `hokma start`); false means "restart it yourself". */
  restarting: boolean;
}

export type UpdatePhase = "idle" | "updating" | "restarting" | "restart_required" | "error";
export type UpdateStage = "download" | "unpack" | "python" | "dependencies" | "verify" | "switch";

/** GET /api/update/state */
export interface UpdateStateData {
  version: string;
  phase: UpdatePhase;
  stage: UpdateStage | null;
  elapsed_s: number | null;
  stage_elapsed_s: number | null;
  error: string | null;
  target_version: string | null;
  checked_at: string | null;
  newer: boolean;
  latest_version: string | null;
  supervised: boolean;
  engine_log: string;
}
