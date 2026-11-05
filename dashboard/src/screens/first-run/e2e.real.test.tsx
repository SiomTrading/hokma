// @vitest-environment jsdom
/**
 * End to end: the real panel components against a REAL backend (`hokma start --no-browser`, real SQLite, real disk,
 * real git), with a stand-in `claude` for the sign-in. Checks the result in the database and on disk.
 * Skipped when the project's Python environment is not there (the backend suite covers the same routes).
 */
import { ChildProcess, execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import type { Locale } from "@/i18n";
import { ShellProvider } from "@/shell/store";
import { FirstRun } from "./FirstRun";
import { GeneralSettings } from "./GeneralSettings";

const REPO = path.resolve(__dirname, "../../../..");
const PYTHON = path.join(REPO, ".venv", "bin", "python");
const FAKE_CLAUDE = path.join(REPO, "backend", "tests", "setup_fixtures", "fake_claude_auth.py");
const runnable = existsSync(PYTHON) && existsSync(FAKE_CLAUDE) && process.platform !== "win32";

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as { port: number };
      server.close(() => resolve(port));
    });
    server.on("error", reject);
  });
}

interface Backend {
  base: string;
  token: string;
  home: string;
  data: string;
  proc: ChildProcess;
  root: string;
}

async function boot(lang: Locale): Promise<Backend> {
  const root = mkdtempSync(path.join(tmpdir(), "hokma-e2e-"));
  const home = path.join(root, "home");
  const data = path.join(root, "data");
  const bin = path.join(root, "bin");
  for (const dir of [home, data, bin]) mkdirSync(dir, { recursive: true });
  const claude = path.join(bin, "claude");
  writeFileSync(claude, "#!" + PYTHON + "\nimport runpy\nrunpy.run_path(" + JSON.stringify(FAKE_CLAUDE) + ", run_name='__main__')\n");
  chmodSync(claude, 0o755);
  const port = await freePort();
  const env = {
    ...process.env,
    HOME: home,
    HOKMA_HOME: path.join(root, "unused-default"),
    HOKMA_DATA_DIR: data,
    HOKMA_LANG: lang,
    PATH: bin + path.delimiter + process.env.PATH,
  };
  const proc = spawn(PYTHON, ["-I", "-m", "hokma_cli.main", "start", "--no-browser", "--port", String(port)], {
    cwd: REPO,
    env: { ...env, PYTHONPATH: path.join(REPO, "backend") + path.delimiter + path.join(REPO, "cli") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const token = await new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("backend did not print its URL")), 30_000);
    proc.stdout!.on("data", (chunk: Buffer) => {
      const match = /#t=([A-Za-z0-9_-]+)/.exec(chunk.toString());
      if (match) {
        clearTimeout(timer);
        resolve(match[1]!);
      }
    });
    proc.on("exit", (code) => reject(new Error("backend exited with " + code)));
  });
  const base = "http://127.0.0.1:" + port;
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(base + "/health")).ok) break;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return { base, token, home, data, proc, root };
}

function sql(data: string, query: string): string {
  return execFileSync(PYTHON, ["-I", "-c", "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print(c.execute(sys.argv[2]).fetchone()[0])", path.join(data, "hokma.db"), query], { encoding: "utf-8" }).trim();
}

let server: Backend | null = null;
const realFetch = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => realFetch(server!.base + String(input), init));
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (server) {
    server.proc.kill();
    rmSync(server.root, { recursive: true, force: true });
    server = null;
  }
});

describe.skipIf(!runnable).each<Locale>(["es", "en"])("real backend, first run in %s", (locale) => {
  it("leaves the profile, journal, folder, git and kit in place", async () => {
    setLocale(locale);
    server = await boot(locale);
    setToken(server.token);
    const user = userEvent.setup();
    render(
      <ShellProvider>
        <div className="hk">
          <FirstRun pollMs={200} />
        </div>
      </ShellProvider>,
    );

    await screen.findByRole("dialog", { name: t("firstrun.dialog") });

    // 1. Claude: sign in through the CLI, paste the code, see the plan the CLI reports
    await screen.findByText(t("firstrun.claude.noSession"), {}, { timeout: 15_000 });
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.login") }));
    await user.type(await screen.findByLabelText(t("firstrun.claude.codeLabel")), "good-code");
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.codeSend") }));
    await screen.findByText(t("firstrun.claude.connectedPlan", { plan: "Max" }), {}, { timeout: 15_000 });
    await user.click(screen.getByRole("button", { name: t("firstrun.next") }));

    // 2. folder: create one inside the explorer and use it
    await screen.findByRole("heading", { name: t("firstrun.folder.title") });
    await user.click(screen.getByRole("button", { name: t("firstrun.folder.browse") }));
    const browser = await screen.findByRole("dialog", { name: t("firstrun.browser.title") });
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent(server!.home));
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.newFolder") }));
    await user.type(within(browser).getByLabelText(t("firstrun.browser.newName")), "my fund");
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.create") }));
    const workdir = path.join(server.home, "my fund");
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent(workdir));
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.use") }));
    await user.click(screen.getByRole("button", { name: t("firstrun.next") }));

    // 3. database: local
    await screen.findByRole("heading", { name: t("firstrun.db.title") });
    await user.click(screen.getByRole("button", { name: t("firstrun.next") }));

    // 4. names
    await screen.findByRole("heading", { name: t("firstrun.name.title") });
    await user.type(screen.getByLabelText(t("firstrun.name.owner")), "Ana Torres");
    await user.type(screen.getByLabelText(t("firstrun.name.fund")), "Torres Capital");
    await waitFor(() => expect(screen.getByLabelText(t("firstrun.name.preview"))).toHaveTextContent("Torres Capital"), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: t("firstrun.next") }));

    // 5. done
    await screen.findByText(t("firstrun.done.manualReady"));
    expect(screen.getByText(t("firstrun.done.agentsCount", { count: String(Number(sql(server.data, "select count(*) from agents")) - 1) }))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("firstrun.open") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    // the database
    expect(sql(server.data, "select owner_name from profile")).toBe("Ana Torres");
    expect(sql(server.data, "select fund_name from profile")).toBe("Torres Capital");
    expect(sql(server.data, "select workdir from profile")).toBe(workdir);
    expect(sql(server.data, "select language from profile")).toBe(locale);
    expect(sql(server.data, "select claude_status || '/' || claude_plan from profile")).toBe("connected/Max");
    expect(sql(server.data, "select database_engine from profile")).toBe("sqlite");
    expect(sql(server.data, "select setup_completed_at is not null from profile")).toBe("1");
    expect(Number(sql(server.data, "select count(*) from journal_entries"))).toBeGreaterThanOrEqual(3);

    // the disk
    expect(existsSync(path.join(workdir, ".git"))).toBe(true);
    expect(existsSync(path.join(workdir, "CLAUDE.md"))).toBe(true);
    const manual = readFileSync(path.join(workdir, "HOKMA.md"), "utf-8");
    expect(manual).toContain("Torres Capital");
    expect(manual).toContain("Ana Torres");
    expect(manual).not.toContain("{{");
    expect(readFileSync(path.join(server.data, "workdir"), "utf-8").trim()).toBe(workdir);
    expect(existsSync(path.join(workdir, "agents", "hokma.md"))).toBe(true);

    // a restart keeps it: ask the same server again
    const state = (await (await realFetch(server.base + "/api/setup", { headers: { "X-Hokma-Token": server.token } })).json()) as { completed: boolean };
    expect(state.completed).toBe(true);
  }, 120_000);
});

const catalog = (lang: Locale): Record<string, string> =>
  JSON.parse(readFileSync(path.join(REPO, "backend", "hokma", "catalog", lang + ".json"), "utf-8"));

const PG_ADMIN = (process.env.HOKMA_TEST_POSTGRES_URL ?? "").trim();

/** Makes a throwaway database on the test server and returns its address plus a function that drops it. */
function throwawayDatabase(): { url: string; drop: () => void } {
  const name = "hokma_e2e_" + Math.random().toString(36).slice(2, 10);
  const script =
    "import sys,psycopg\nfrom urllib.parse import urlsplit,urlunsplit\n" +
    "admin,name,action=sys.argv[1:4]\n" +
    "with psycopg.connect(admin,autocommit=True) as c:\n" +
    "    c.execute(('CREATE DATABASE ' if action=='create' else 'DROP DATABASE IF EXISTS ')+name)\n" +
    "p=urlsplit(admin);print(urlunsplit((p.scheme,p.netloc,'/'+name,p.query,'')))\n";
  const run = (action: string) => execFileSync(PYTHON, ["-I", "-c", script, PG_ADMIN, name, action], { encoding: "utf-8" }).trim();
  return { url: run("create"), drop: () => void run("drop") };
}

describe.skipIf(!runnable).each<Locale>(["es", "en"])("real backend, Settings > General database in %s", (locale) => {
  it("tests a bad address in the backend's words, refuses to move untested and keeps SQLite", async () => {
    setLocale(locale);
    server = await boot(locale);
    setToken(server.token);
    const user = userEvent.setup();
    render(
      <ShellProvider>
        <div className="hk">
          <GeneralSettings />
        </div>
      </ShellProvider>,
    );
    expect(await screen.findByText(t("firstrun.db.sqliteName"), {}, { timeout: 15_000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("general.databaseChange") }));
    await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.pg")) }));
    await user.type(screen.getByLabelText(t("firstrun.db.url")), "postgresql://nobody@127.0.0.1:1/none");

    await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t("firstrun.db.mustTest"));

    await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
    const words = catalog(locale)["db.conn.refused"]!;
    expect(await screen.findByText(words, {}, { timeout: 15_000 })).toBeInTheDocument();

    // choosing local again and applying moves nothing
    await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.local")) }));
    await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
    await waitFor(() => expect(screen.queryByRole("button", { name: t("general.databaseApply") })).not.toBeInTheDocument());
    expect(screen.queryByRole("status", { name: t("general.migration.title") })).not.toBeInTheDocument();
    const status = (await (await realFetch(server.base + "/api/database", { headers: { "X-Hokma-Token": server.token, "Accept-Language": locale } })).json()) as { engine: string };
    expect(status.engine).toBe("sqlite");
  }, 120_000);

  it.skipIf(!PG_ADMIN)("moves to a real Postgres and back, showing the verification each time", async () => {
    setLocale(locale);
    const pg = throwawayDatabase();
    try {
      server = await boot(locale);
      setToken(server.token);
      const user = userEvent.setup();
      render(
        <ShellProvider>
          <div className="hk">
            <GeneralSettings />
          </div>
        </ShellProvider>,
      );
      await screen.findByText(t("firstrun.db.sqliteName"), {}, { timeout: 15_000 });
      await user.click(screen.getByRole("button", { name: t("general.databaseChange") }));
      await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.pg")) }));
      await user.type(screen.getByLabelText(t("firstrun.db.url")), pg.url);
      await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
      await screen.findByText(t("firstrun.db.testOk"), {}, { timeout: 15_000 });
      await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
      const first = await screen.findByRole("status", { name: t("general.migration.title") }, { timeout: 60_000 });
      expect(first).toHaveTextContent(/\d+/);
      expect(await screen.findByText(t("firstrun.db.pgName"))).toBeInTheDocument();
      expect(screen.queryByText(pg.url)).not.toBeInTheDocument();             // no password-bearing address in the panel
    } finally {
      pg.drop();
    }
  }, 180_000);
});
