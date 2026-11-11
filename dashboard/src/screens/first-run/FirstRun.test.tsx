import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import type { Locale } from "@/i18n";
import { ShellProvider } from "@/shell/store";
import { useShell } from "@/shell/useShell";
import { FirstRun } from "./FirstRun";
import { GeneralSettings } from "./GeneralSettings";
import type { SetupState } from "./types";

/** A stateful stand-in for the backend: it answers like the real routes and remembers what was sent. */
class FakeBackend {
  calls: Array<{ method: string; path: string; body: unknown }> = [];
  claude: "connected" | "no_session" | "no_cli" = "no_session";
  loginStarted = false;
  /** What `claude auth login` printed when it failed: the real route sends it as the error message. */
  loginFailure: string | null = null;
  loginDied: string | null = null;
  pgOk = false;
  pgNotEmpty = false;
  failWorkdir: string | null = null;
  /** Like profile.permission_mode: a new installation starts in "ask" until the setup closes. */
  permission = { mode: "ask", intro_seen: false };
  state: SetupState = {
    completed: false,
    profile: null,
    owner: { owner_name: "", fund_name: "" },
    workdir: {
      path: "/home/ana/Hokma",
      exists: false,
      git: false,
      manual: false,
      options: [
        { id: "documents", provider: null, path: "/home/ana/Documents/Hokma" },
        { id: "sync", provider: "Dropbox", path: "/home/ana/Dropbox/Hokma" },
      ],
    },
    database: { engine: "sqlite", location: null },
    agents: { total: 0 },
    claude: { state: "unknown", plan: null },
    version: "0.0.5",
  };

  json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }

  profile(): NonNullable<SetupState["profile"]> {
    return (this.state.profile ??= {
      owner_name: "",
      fund_name: null,
      language: "es",
      theme: "dark",
      workdir: null,
      database_engine: "sqlite",
      claude_status: "unknown",
      claude_plan: null,
      setup_completed_at: null,
    });
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname + url.search, body });
    const route = `${method} ${url.pathname}`;
    switch (route) {
      case "GET /api/setup":
        return this.json(this.state);
      case "GET /api/claude":
        if (this.loginStarted && this.claude === "no_session" && !this.loginDied) this.claude = "connected";
        return this.json({
          state: this.claude,
          installed: this.claude !== "no_cli",
          plan: this.claude === "connected" ? "Max" : null,
          version: "9.9.9",
          login: {
            running: this.loginStarted && !this.loginDied,
            url: this.loginStarted ? "https://claude.com/cai/oauth/authorize?x=1" : null,
            ...(this.loginDied ? { error: this.loginDied } : {}),
          },
        });
      case "POST /api/claude/login":
        if (this.loginFailure) return this.json({ detail: { code: "login_exited", message: this.loginFailure } }, 500);
        this.loginStarted = true;
        return this.json({ running: true, url: "https://claude.com/cai/oauth/authorize?x=1" });
      case "GET /api/fs/dirs":
        // like the real route: a folder that does not exist yet is browsed from its nearest existing parent
        if (url.searchParams.get("path") === "/home/ana/Backups") {
          return this.json({ path: "/home/ana/Backups", parent: "/home/ana", dirs: [], roots: [] });
        }
        return this.json({ path: "/home/ana", parent: "/home", dirs: [{ name: "Backups", path: "/home/ana/Backups" }], roots: [] });
      case "POST /api/workdir": {
        if (this.failWorkdir) return this.json({ detail: { code: "path_protected", message: this.failWorkdir } }, 400);
        const path = (body as { path: string }).path;
        this.state.workdir = { ...this.state.workdir, path, exists: true, git: true, manual: true };
        this.state.agents = { total: 5 };
        if (this.state.profile) this.state.profile.workdir = path;
        return this.json({ path, created: true, git_initialized: true, git: true });
      }
      case "POST /api/database/test":
        return this.json({ ok: this.pgOk, code: this.pgOk ? "ok" : "auth", message: this.pgOk ? "" : "password authentication failed", notes: [] });
      case "POST /api/database/migrate": {
        const b = body as { target: "sqlite" | "postgres"; url?: string; replace: boolean };
        if (this.pgNotEmpty && !b.replace) {
          return this.json({ detail: { code: "target_not_empty", message: "The destination already has data." } }, 409);
        }
        this.state.database = { engine: b.target, location: b.target === "postgres" ? "postgresql://u@h:5432/db" : null };
        return this.json({
          ok: true,
          engine: b.target,
          location: this.state.database.location,
          rows: 42,
          counts: { profile: 1, agents: 41 },
          schema_version: 3,
          notes: ["roles created"],
          message: "Done. Hokma now uses the new database.",
        });
      }
      case "GET /api/owner/preview": {
        const owner = url.searchParams.get("owner_name") || "the owner";
        return this.json({ text: "# Hokma: " + (url.searchParams.get("fund_name") || "the fund") + "\n\nFor " + owner });
      }
      case "POST /api/owner": {
        const b = body as { owner_name: string; fund_name: string; language?: "es" | "en"; theme?: "dark" | "light" };
        const p = this.profile();
        p.owner_name = b.owner_name.trim();
        p.fund_name = b.fund_name.trim() || null;
        if (b.language) p.language = b.language;
        if (b.theme) p.theme = b.theme;
        p.workdir ??= this.state.workdir.path;
        this.state.owner = { owner_name: p.owner_name, fund_name: p.fund_name ?? "" };
        return this.json(this.state);
      }
      case "GET /api/protections":
        return this.json({ always_on: true, rules: ["delete", "hokma_internal", "outside_workdir", "secrets", "real_money"].map((id) => ({ id, title: id })) });
      case "GET /api/profile/permission-mode":
        return this.json(this.permission);
      case "PUT /api/profile/permission-mode": {
        const b = body as { mode?: string; intro_seen?: boolean };
        const before = this.permission.mode;
        if (b.mode) this.permission = { mode: b.mode, intro_seen: true };
        else if (b.intro_seen !== undefined) this.permission = { ...this.permission, intro_seen: b.intro_seen };
        return this.json({ ...this.permission, changed: before !== this.permission.mode, turns_running: 0 });
      }
      case "POST /api/setup":
        // like the server: a new installation that did not choose starts in "do everything without asking"
        if (!this.state.completed && !this.permission.intro_seen) this.permission = { mode: "auto", intro_seen: true };
        this.profile().setup_completed_at = "2026-11-05 12:00:00";
        this.state.completed = true;
        return this.json(this.state);
      case "PATCH /api/profile": {
        const b = body as { theme?: "dark" | "light"; language?: "es" | "en"; database_engine?: "sqlite" | "postgres" };
        if (!this.state.profile) return this.json({ detail: { code: "no_profile", message: "no profile" } }, 409);
        if (b.database_engine) this.state.profile.database_engine = b.database_engine;
        if (b.theme) this.state.profile.theme = b.theme;
        if (b.language) this.state.profile.language = b.language;
        return this.json(this.state);
      }
      default:
        return this.json({ detail: "unexpected " + route }, 404);
    }
  };

  posts(path: string) {
    return this.calls.filter((c) => c.method !== "GET" && c.path === path);
  }
}

let backend: FakeBackend;

function Harness({ children }: { children: React.ReactNode }) {
  return (
    <ShellProvider>
      <div className="hk">{children}</div>
    </ShellProvider>
  );
}

function ActiveTab() {
  const shell = useShell();
  return <span data-testid="active-tab">{shell.activeKey}</span>;
}

beforeEach(() => {
  backend = new FakeBackend();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
  setToken("test-session-token-0123456789");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const nextButton = () => screen.getByRole("button", { name: t("firstrun.next") });

describe.each<Locale>(["es", "en"])("first run, complete flow in %s", (locale) => {
  beforeEach(() => setLocale(locale));

  it("goes through Claude, folder, database, name and opens the fund", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun pollMs={20} />
        <ActiveTab />
      </Harness>,
    );

    // opens by itself, without any license step
    const dialog = await screen.findByRole("dialog", { name: t("firstrun.dialog") });
    expect(within(dialog).getByText(t("firstrun.welcome"))).toBeInTheDocument();
    expect(within(dialog).queryByText(/licen/i)).not.toBeInTheDocument();

    // 1. Claude
    expect(await screen.findByText(t("firstrun.claude.noSession"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.login") }));
    expect(await screen.findByText(t("firstrun.claude.connectedPlan", { plan: "Max" }))).toBeInTheDocument();
    await user.click(nextButton());

    // 2. folder: the three-way choice computed by the backend, the explorer and the typed path
    expect(await screen.findByRole("heading", { name: t("firstrun.folder.title") })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: new RegExp(t("firstrun.folder.documents")) })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: new RegExp(t("firstrun.folder.sync", { provider: "Dropbox" })) })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("firstrun.folder.browse") }));
    const browser = await screen.findByRole("dialog", { name: t("firstrun.browser.title") });
    await user.click(await within(browser).findByRole("button", { name: "Backups" }));
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent("/home/ana/Backups"));
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.use") }));
    expect(screen.getByLabelText(t("firstrun.folder.chosen"))).toHaveValue("/home/ana/Backups");
    await user.click(nextButton());

    // 3. database: local by default
    expect(await screen.findByRole("heading", { name: t("firstrun.db.title") })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.local")) })).toHaveAttribute("aria-checked", "true");
    await user.click(nextButton());

    // 4. name: required, limited to 60, live preview
    expect(await screen.findByRole("heading", { name: t("firstrun.name.title") })).toBeInTheDocument();
    const owner = screen.getByLabelText(t("firstrun.name.owner"));
    expect(owner).toHaveAttribute("maxLength", "60");
    expect(screen.getByLabelText(t("firstrun.name.fund"))).toHaveAttribute("maxLength", "60");
    expect(nextButton()).toBeDisabled();
    await user.type(owner, "Ana Torres");
    await user.type(screen.getByLabelText(t("firstrun.name.fund")), "Torres Capital");
    await waitFor(() => expect(screen.getByLabelText(t("firstrun.name.preview"))).toHaveTextContent("Hokma: Torres Capital"));
    await user.click(nextButton());

    // 5. done: real cards
    expect(await screen.findByText(t("firstrun.done.manualReady"))).toBeInTheDocument();
    expect(screen.getByText("/home/ana/Backups")).toBeInTheDocument();
    expect(screen.getByText(t("firstrun.db.sqliteName"))).toBeInTheDocument();
    expect(screen.getByText(t("firstrun.done.agentsCount", { count: "4" }))).toBeInTheDocument();
    // P1: the new installation shows the switch already on, and nothing about keys or providers
    const modeCard = await screen.findByTestId("firstrun-mode");
    expect(within(modeCard).getByRole("switch", { name: t("permissions.mode.auto") })).toHaveAttribute("aria-checked", "true");
    expect(within(modeCard).queryByRole("textbox")).not.toBeInTheDocument();      // no key to type here
    expect(within(modeCard).queryByText(/API|provider|proveedor/i)).not.toBeInTheDocument();
    expect(backend.posts("/api/profile/permission-mode")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: t("firstrun.open") }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("active-tab")).toHaveTextContent("chat");

    // what reached the backend, in order and with the right content
    expect(backend.posts("/api/workdir")).toEqual([{ method: "POST", path: "/api/workdir", body: { path: "/home/ana/Backups" } }]);
    expect(backend.posts("/api/database/migrate")).toHaveLength(0);            // local is already in use: nothing to move
    expect(backend.posts("/api/owner")[0]!.body).toEqual({ owner_name: "Ana Torres", fund_name: "Torres Capital", language: locale, theme: "dark" });
    expect(backend.posts("/api/setup")).toHaveLength(1);
    expect(backend.state.completed).toBe(true);
    expect(backend.permission).toEqual({ mode: "auto", intro_seen: true });
    const order = backend.calls.filter((c) => c.method === "POST").map((c) => c.path);
    expect(order.indexOf("/api/workdir")).toBeLessThan(order.indexOf("/api/owner"));
    expect(order.indexOf("/api/owner")).toBeLessThan(order.indexOf("/api/setup"));
  });
});

describe("first run, permission mode card", () => {
  beforeEach(() => setLocale("en"));

  /** The name is the only step missing, so the setup opens on it and then goes to the "Ready" card. */
  async function reachDone(user: ReturnType<typeof userEvent.setup>) {
    backend.claude = "connected";
    backend.state.workdir = { ...backend.state.workdir, exists: true, git: true, manual: true };
    backend.state.profile = { ...backend.profile(), owner_name: "", workdir: "/home/ana/Hokma" };
    render(
      <Harness>
        <FirstRun pollMs={20} />
      </Harness>,
    );
    await screen.findByRole("dialog", { name: t("firstrun.dialog") });
    await user.type(await screen.findByLabelText(t("firstrun.name.owner")), "Ana");
    await user.click(screen.getByRole("button", { name: t("firstrun.next") }));
  }

  it("the owner can switch it off before starting, and that choice is kept", async () => {
    const user = userEvent.setup();
    await reachDone(user);
    const card = await screen.findByTestId("firstrun-mode");
    const toggle = within(card).getByRole("switch", { name: t("permissions.mode.auto") });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
    expect(backend.posts("/api/profile/permission-mode")[0]!.body).toEqual({ mode: "ask" });
    await user.click(screen.getByRole("button", { name: t("firstrun.open") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(backend.permission).toEqual({ mode: "ask", intro_seen: true });
  });
});

describe("first run, edge cases", () => {
  beforeEach(() => setLocale("es"));

  it("stays closed when the setup is already complete", async () => {
    backend.state.completed = true;
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await waitFor(() => expect(backend.calls.length).toBeGreaterThan(0));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("an owner who updated from an earlier version is not asked again for what exists", async () => {
    backend.claude = "connected";
    backend.state.workdir = { ...backend.state.workdir, exists: true, git: true, manual: true };
    backend.state.owner = { owner_name: "Ana", fund_name: "Fondo" };
    backend.profile().owner_name = "Ana";
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await waitFor(() => expect(backend.posts("/api/setup")).toHaveLength(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("an owner who updated is asked only for what is missing, never the name", async () => {
    backend.claude = "no_session";
    backend.state.workdir = { ...backend.state.workdir, exists: true, git: true, manual: true };
    backend.state.owner = { owner_name: "Ana", fund_name: "Fondo" };
    backend.profile().owner_name = "Ana";
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    const steps = await screen.findByLabelText(t("firstrun.steps"));
    expect(steps).toHaveTextContent(t("firstrun.step.claude"));
    expect(steps).toHaveTextContent(t("firstrun.step.done"));
    for (const gone of ["folder", "database", "name"]) expect(steps).not.toHaveTextContent(t(`firstrun.step.${gone}`));
    expect(screen.getByText(t("firstrun.returning"))).toBeInTheDocument();
    expect(backend.posts("/api/setup")).toHaveLength(0);
  });

  it("stays closed (and quiet) when the backend cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await act(async () => {});
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows installation instructions without Claude Code, lets you check again and continue", async () => {
    backend.claude = "no_cli";
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    expect(await screen.findByText(t("firstrun.claude.noCli"))).toBeInTheDocument();
    expect(screen.getByText(t("firstrun.claude.installTitle"))).toBeInTheDocument();
    expect(screen.getByText(t("firstrun.claude.skipWarn"))).toBeInTheDocument();
    backend.claude = "connected";
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.recheck") }));
    expect(await screen.findByText(t("firstrun.claude.connectedPlan", { plan: "Max" }))).toBeInTheDocument();
    expect(nextButton()).toBeEnabled();
  });

  it("shows what the Claude sign-in printed when it fails to start", async () => {
    backend.loginFailure = "Sign-in ended with an error (code 3). Claude Code said: Error: OAuth flow failed";
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.login") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Error: OAuth flow failed");
  });

  it("shows what the Claude sign-in printed when it dies while waiting for the code", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun pollMs={20} />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    backend.loginDied = "Sign-in ended with an error (code 1). Claude Code said: Invalid code";
    backend.claude = "no_session";
    backend.loginStarted = true;
    await user.click(screen.getByRole("button", { name: t("firstrun.claude.recheck") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid code");
  });

  it("can continue without a session", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(nextButton());
    expect(await screen.findByRole("heading", { name: t("firstrun.folder.title") })).toBeInTheDocument();
  });

  it("shows the backend's refusal for a protected folder and stays on the step", async () => {
    backend.failWorkdir = "That folder is part of Hokma's internal data.";
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(nextButton());
    await screen.findByRole("heading", { name: t("firstrun.folder.title") });
    await user.click(nextButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("internal data");
    expect(screen.getByRole("heading", { name: t("firstrun.folder.title") })).toBeInTheDocument();
  });

  it("Postgres needs a successful connection test before continuing", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(nextButton());
    await screen.findByRole("heading", { name: t("firstrun.folder.title") });
    await user.click(nextButton());
    await screen.findByRole("heading", { name: t("firstrun.db.title") });
    await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.pg")) }));
    await user.type(screen.getByLabelText(t("firstrun.db.url")), "postgresql://u@h:5432/db");

    await user.click(nextButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(t("firstrun.db.mustTest"));

    await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
    expect(await screen.findByText("password authentication failed")).toBeInTheDocument();
    backend.pgOk = true;
    await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
    expect(await screen.findByText(t("firstrun.db.testOk"))).toBeInTheDocument();
    await user.click(nextButton());
    expect(await screen.findByRole("heading", { name: t("firstrun.name.title") })).toBeInTheDocument();
    expect(backend.posts("/api/database/migrate")[0]!.body).toEqual({ target: "postgres", url: "postgresql://u@h:5432/db", replace: false });
    expect(backend.state.database.engine).toBe("postgres");
  });

  it("asks before overwriting a Postgres that already has data, and only then sends replace", async () => {
    backend.pgOk = true;
    backend.pgNotEmpty = true;
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(nextButton());
    await screen.findByRole("heading", { name: t("firstrun.folder.title") });
    await user.click(nextButton());
    await screen.findByRole("heading", { name: t("firstrun.db.title") });
    await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.pg")) }));
    await user.type(screen.getByLabelText(t("firstrun.db.url")), "postgresql://u@h:5432/db");
    await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
    await screen.findByText(t("firstrun.db.testOk"));
    await user.click(nextButton());
    const confirm = await screen.findByRole("dialog", { name: t("firstrun.db.replaceTitle") });
    expect(within(confirm).getByText("The destination already has data.")).toBeInTheDocument();
    expect(backend.posts("/api/database/migrate")).toHaveLength(1);
    await user.click(within(confirm).getByRole("button", { name: t("firstrun.db.replaceConfirm") }));
    expect(await screen.findByRole("heading", { name: t("firstrun.name.title") })).toBeInTheDocument();
    expect(backend.posts("/api/database/migrate").map((c) => (c.body as { replace: boolean }).replace)).toEqual([false, true]);
  });

  it("the folder explorer creates a new folder", async () => {
    const mk = vi.fn();
    const base = backend.handle;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/fs/dirs" && init?.method === "POST") {
          mk(JSON.parse(String(init.body)));
          return backend.json({ path: "/home/ana/New" }, 201);
        }
        return base(input, init);
      }),
    );
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
      </Harness>,
    );
    await screen.findByText(t("firstrun.claude.noSession"));
    await user.click(nextButton());
    await user.click(await screen.findByRole("button", { name: t("firstrun.folder.browse") }));
    const browser = await screen.findByRole("dialog", { name: t("firstrun.browser.title") });
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent("/home/ana"));
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.newFolder") }));
    await user.type(within(browser).getByLabelText(t("firstrun.browser.newName")), "New");
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.create") }));
    await waitFor(() => expect(mk).toHaveBeenCalledWith({ parent: "/home/ana", name: "New" }));
  });
});

describe("Settings > General", () => {
  beforeEach(() => {
    setLocale("es");
    backend.state.completed = true;
    backend.state.profile = {
      owner_name: "Ana Torres",
      fund_name: "Torres Capital",
      language: "es",
      theme: "dark",
      workdir: "/home/ana/Hokma",
      database_engine: "sqlite",
      claude_status: "connected",
      claude_plan: "Max",
      setup_completed_at: "2026-11-01 10:00:00",
    };
    backend.state.owner = { owner_name: "Ana Torres", fund_name: "Torres Capital" };
    backend.state.workdir = { ...backend.state.workdir, exists: true, git: true, manual: true };
  });

  it("shows the real values and saves new names", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <GeneralSettings />
      </Harness>,
    );
    const owner = await screen.findByLabelText(t("general.owner"));
    expect(owner).toHaveValue("Ana Torres");
    expect(screen.getByLabelText(t("general.fund"))).toHaveValue("Torres Capital");
    expect(screen.getByLabelText(t("general.folder"))).toHaveValue("/home/ana/Hokma");
    expect(screen.getByText(t("firstrun.db.sqliteName"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("general.saveNames") })).toBeDisabled();
    await user.clear(owner);
    await user.type(owner, "Ana Maria");
    await user.click(screen.getByRole("button", { name: t("general.saveNames") }));
    await waitFor(() => expect(backend.posts("/api/owner")).toHaveLength(1));
    expect(backend.posts("/api/owner")[0]!.body).toMatchObject({ owner_name: "Ana Maria", fund_name: "Torres Capital" });
    expect(backend.state.profile!.owner_name).toBe("Ana Maria");
  });

  it("changes the folder with the explorer", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <GeneralSettings />
      </Harness>,
    );
    await screen.findByLabelText(t("general.owner"));
    await user.click(screen.getByRole("button", { name: t("general.folderChange") }));
    const browser = await screen.findByRole("dialog", { name: t("firstrun.browser.title") });
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent("/home/ana"));
    await user.click(within(browser).getByRole("button", { name: "Backups" }));
    await waitFor(() => expect(within(browser).getByTestId("browser-path")).toHaveTextContent("/home/ana/Backups"));
    await user.click(within(browser).getByRole("button", { name: t("firstrun.browser.use") }));
    await waitFor(() => expect(backend.posts("/api/workdir")).toHaveLength(1));
    expect(backend.posts("/api/workdir")[0]!.body).toEqual({ path: "/home/ana/Backups" });
  });

  it("light mode is saved in the profile", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun />
        <GeneralSettings />
      </Harness>,
    );
    await user.click(await screen.findByRole("switch", { name: t("general.light") }));
    await waitFor(() => expect(backend.posts("/api/profile")).toHaveLength(1));
    expect(backend.posts("/api/profile")[0]!.body).toEqual({ theme: "light" });
    expect(backend.state.profile!.theme).toBe("light");
  });

  it("'Run again' reopens the setup with the current values", async () => {
    const user = userEvent.setup();
    render(
      <Harness>
        <FirstRun pollMs={20} />
        <GeneralSettings />
      </Harness>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: t("general.relaunch") }));
    const dialog = await screen.findByRole("dialog", { name: t("firstrun.dialog") });
    await waitFor(() => expect(within(dialog).getByText(t("firstrun.claude.noSession"))).toBeInTheDocument());
    await user.click(within(dialog).getByRole("button", { name: t("firstrun.next") }));
    expect(await screen.findByLabelText(t("firstrun.folder.chosen"))).toHaveValue("/home/ana/Hokma");
    await user.click(screen.getByRole("button", { name: t("firstrun.back") }));
    await user.click(screen.getByRole("button", { name: t("firstrun.cancel") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(backend.state.completed).toBe(true);
  });

  it("changes the database, asks before replacing and shows the verification", async () => {
    backend.pgOk = true;
    backend.pgNotEmpty = true;
    const user = userEvent.setup();
    render(
      <Harness>
        <GeneralSettings />
      </Harness>,
    );
    await screen.findByLabelText(t("general.owner"));
    await user.click(screen.getByRole("button", { name: t("general.databaseChange") }));
    await user.click(screen.getByRole("radio", { name: new RegExp(t("firstrun.db.pg")) }));
    await user.type(screen.getByLabelText(t("firstrun.db.url")), "postgresql://u@h:5432/db");
    await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t("firstrun.db.mustTest"));
    await user.click(screen.getByRole("button", { name: t("firstrun.db.test") }));
    await screen.findByText(t("firstrun.db.testOk"));
    await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
    const confirm = await screen.findByRole("dialog", { name: t("firstrun.db.replaceTitle") });
    await user.click(within(confirm).getByRole("button", { name: t("firstrun.cancel") }));
    expect(backend.state.database.engine).toBe("sqlite");
    await user.click(screen.getByRole("button", { name: t("general.databaseApply") }));
    await user.click(within(await screen.findByRole("dialog", { name: t("firstrun.db.replaceTitle") })).getByRole("button", { name: t("firstrun.db.replaceConfirm") }));
    const result = await screen.findByRole("status", { name: t("general.migration.title") });
    expect(result).toHaveTextContent("Done. Hokma now uses the new database.");
    expect(result).toHaveTextContent(t("general.migration.summary", { rows: "42", tables: "2", version: "3" }));
    expect(screen.getByText(t("firstrun.db.pgName"))).toBeInTheDocument();
    expect(backend.state.profile!.database_engine).toBe("postgres");
  });

  it("offers no API provider anywhere", async () => {
    render(
      <Harness>
        <GeneralSettings />
      </Harness>,
    );
    await screen.findByLabelText(t("general.owner"));
    expect(screen.queryByText(/proveedor|provider/i)).not.toBeInTheDocument();
  });
});
