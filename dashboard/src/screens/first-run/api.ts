import { api } from "@/api";
import type { ClaudeDto, DbChoice, DirListing, LoginInfo, MigrationDto, SetupState, WorkdirResult } from "./types";

export const setupApi = {
  state: () => api.get<SetupState>("/api/setup"),
  complete: () => api.post<SetupState>("/api/setup"),
  claude: () => api.get<ClaudeDto>("/api/claude"),
  login: () => api.post<LoginInfo>("/api/claude/login"),
  sendCode: (code: string) => api.post<{ sent: boolean }>("/api/claude/login/code", { code }),
  dirs: (path: string | null) => api.get<DirListing>(path ? "/api/fs/dirs?path=" + encodeURIComponent(path) : "/api/fs/dirs"),
  makeDir: (parent: string, name: string) => api.post<{ path: string }>("/api/fs/dirs", { parent, name }),
  setWorkdir: (path: string) => api.post<WorkdirResult>("/api/workdir", { path }),
  testDatabase: (url: string) => api.post<{ ok: boolean; code?: string; message: string }>("/api/database/test", { url }),
  migrateDatabase: (target: DbChoice, url: string | undefined, replace: boolean) =>
    api.post<MigrationDto>("/api/database/migrate", { target, url, replace }, { timeoutMs: 0 }),
  preview: (owner: string, fund: string) =>
    api.get<{ text: string }>("/api/owner/preview?owner_name=" + encodeURIComponent(owner) + "&fund_name=" + encodeURIComponent(fund)),
  setOwner: (body: { owner_name: string; fund_name: string; language?: "es" | "en"; theme?: "dark" | "light" }) =>
    api.post<SetupState>("/api/owner", body),
  patchProfile: (body: { theme?: "dark" | "light"; language?: "es" | "en"; database_engine?: DbChoice }) =>
    api.patch<SetupState>("/api/profile", body),
};

/**
 * Moves the fund to `engine` when it is not the one in use (the database module copies the data), then tells the
 * profile. Returns the verification report, or null when nothing moved. A destination that already holds data
 * surfaces as an ApiError with code `target_not_empty`, so the panel asks before overwriting it.
 */
export async function switchDatabase(current: DbChoice, engine: DbChoice, url: string | undefined, overwrite: boolean): Promise<MigrationDto | null> {
  if (engine === current) return null;
  const report = await setupApi.migrateDatabase(engine, engine === "postgres" ? url : undefined, overwrite);
  await setupApi.patchProfile({ database_engine: engine }).catch(() => undefined);
  return report;
}
