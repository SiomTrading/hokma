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
  installed: { version: string; installed_at: string | null };
  changelog: ChangelogSection[];
  latest: { version: string; notes: string | null; published_at: string | null } | null;
  latest_error: string | null;
  newer: boolean;
}

export interface UpdateResult {
  status: "updated" | "up-to-date";
  from_version: string;
  to_version: string;
  restart_required: boolean;
}
