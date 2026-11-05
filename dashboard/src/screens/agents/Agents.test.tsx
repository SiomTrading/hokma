import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import type { Locale } from "@/i18n";
import { ShellProvider } from "@/shell/store";
import { AgentsSettings } from "./AgentsSettings";
import { describeSchedule, nextModel, parseWeekly } from "./format";
import { ModelsSettings } from "./ModelsSettings";
import { TasksScreen } from "./TasksScreen";
import type { AgentItem, JobItem, UsageData } from "./types";
import { UsageStatusItem } from "./UsageStatusItem";

function agent(slug: string, name: string, patch: Partial<AgentItem> = {}): AgentItem {
  return {
    slug, name, description: `${name} description`, tier: "balanced", model: "sonnet", status: "active", active: true,
    is_custom: false, is_orchestrator: false, enabled_session: null, role: "specialist", ...patch,
  };
}

/** A stateful stand-in for the backend: it answers like the real routes and remembers what was sent. */
class Backend {
  calls: Array<{ method: string; path: string; body: unknown }> = [];
  agents: AgentItem[] = [
    agent("hokma", "Hokma", { is_orchestrator: true, role: "orchestrator" }),
    agent("vera", "Vera"),
    agent("diego", "Diego", { active: false, status: "off", model: "haiku", tier: "fast" }),
  ];
  usage: UsageData = {
    starts_at: "2026-11-02 00:00:00", ends_at: "2026-11-09 00:00:00", used: 38000, tokens_in: 30000, tokens_out: 8000,
    messages: 4, runs: 1, cap: 100000, pct: 38, by_agent: [{ key: "hokma", tokens_in: 30000, tokens_out: 8000 }],
    by_model: [{ key: "sonnet", tokens_in: 30000, tokens_out: 8000 }], by_day: [],
    claude: { status: "connected", plan: "Max", checked_at: null }, profile_exists: true,
  };
  jobs: JobItem[] = [];
  failWith: { code: string; message: string; status: number } | null = null;
  nextId = 1;

  json(body: unknown, status = 200): Response {
    return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }

  data() {
    const orchestrator = this.agents.find((a) => a.is_orchestrator) ?? null;
    return { orchestrator, agents: this.agents.filter((a) => !a.is_orchestrator), active: this.agents.filter((a) => a.active).length, total: this.agents.length };
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname, body });
    if (this.failWith && method !== "GET") {
      return this.json({ detail: { code: this.failWith.code, message: this.failWith.message } }, this.failWith.status);
    }
    const model = url.pathname.match(/^\/api\/agents\/([^/]+)\/model$/);
    const status = url.pathname.match(/^\/api\/agents\/([^/]+)\/status$/);
    const job = url.pathname.match(/^\/api\/scheduled-jobs\/(\d+)(?:\/(activate|deactivate))?$/);
    if (method === "GET" && url.pathname === "/api/agents") return this.json(this.data());
    if (method === "PUT" && model) {
      const found = this.agents.find((a) => a.slug === model[1])!;
      found.model = body.model;
      return this.json(found);
    }
    if (method === "PUT" && status) {
      const found = this.agents.find((a) => a.slug === status[1])!;
      found.status = body.status;
      found.active = body.status === "active";
      return this.json(found);
    }
    if (method === "POST" && url.pathname === "/api/agents") {
      const created = agent(String(body.name).toLowerCase().replace(/\s+/g, "-"), body.name, { is_custom: true, model: body.model, description: body.description });
      this.agents.push(created);
      return this.json(created, 201);
    }
    if (method === "GET" && url.pathname === "/api/usage/weekly") return this.json(this.usage);
    if (method === "PUT" && url.pathname === "/api/usage/cap") {
      this.usage.cap = body.cap;
      this.usage.pct = body.cap ? Math.round((this.usage.used * 1000) / body.cap) / 10 : null;
      return this.json(this.usage);
    }
    if (method === "GET" && url.pathname === "/api/scheduled-jobs") return this.json({ jobs: this.jobs, running: [] });
    if (method === "POST" && url.pathname === "/api/scheduled-jobs") {
      const created: JobItem = {
        id: this.nextId++, name: body.name, agent: body.agent, agent_name: this.agents.find((a) => a.slug === body.agent)!.name,
        agent_active: true, model: "sonnet", prompt: body.prompt, kind: body.kind, expr: body.expr, timezone: body.timezone,
        active: false, approved: false, next_run_at: null, last_run_at: null, last: null,
      };
      this.jobs.push(created);
      return this.json(created, 201);
    }
    if (job && method === "POST" && job[2]) {
      const found = this.jobs.find((j) => j.id === Number(job[1]))!;
      found.active = job[2] === "activate";
      found.approved = found.active || found.approved;
      found.next_run_at = found.active ? "2026-11-06 12:30:00" : null;
      return this.json(found);
    }
    if (job && method === "DELETE") {
      this.jobs = this.jobs.filter((j) => j.id !== Number(job[1]));
      return this.json(null, 204);
    }
    return this.json({ detail: "unexpected " + method + " " + url.pathname }, 404);
  };

  sent(method: string, path: string) {
    return this.calls.filter((c) => c.method === method && c.path === path);
  }
}

let backend: Backend;

function mount(ui: React.ReactElement) {
  return render(
    <ShellProvider>
      <div className="hk">{ui}</div>
    </ShellProvider>,
  );
}

beforeEach(() => {
  backend = new Backend();
  vi.stubGlobal("fetch", backend.handle);
  setToken("test-token");
});
afterEach(() => vi.unstubAllGlobals());

describe.each<Locale>(["es", "en"])("agents, usage and scheduled tasks (%s)", (locale) => {
  beforeEach(() => setLocale(locale));

  it("lists the orchestrator and the agents with the real count", async () => {
    mount(<AgentsSettings />);
    expect(await screen.findByText(t("agents.count", { active: 2, total: 3 }))).toBeInTheDocument();
    expect(screen.getByText(`${t("agents.orchestrator")}: Hokma`)).toBeInTheDocument();
    expect(screen.getByText("Vera")).toBeInTheDocument();
    expect(screen.getByText("Diego")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: t("agents.toggle", { name: "Vera" }) })).toBeChecked();
    expect(screen.getByRole("switch", { name: t("agents.toggle", { name: "Diego" }) })).not.toBeChecked();
    expect(screen.getByText(t("agents.state.off"))).toBeInTheDocument();
  });

  it("cycles the model Haiku, Sonnet, Opus and saves it", async () => {
    const user = userEvent.setup();
    mount(<AgentsSettings />);
    await user.click(await screen.findByRole("button", { name: t("agents.model.cycle", { name: "Vera", model: t("agents.model.sonnet") }) }));
    expect(backend.sent("PUT", "/api/agents/vera/model")).toHaveLength(0);
    await user.click(within(await screen.findByRole("dialog", { name: t("agents.opus.title") })).getByRole("button", { name: t("agents.opus.confirm") }));
    await waitFor(() => expect(backend.sent("PUT", "/api/agents/vera/model")).toHaveLength(1));
    expect(backend.sent("PUT", "/api/agents/vera/model")[0]?.body).toEqual({ model: "opus" });
    expect(await screen.findByRole("button", { name: t("agents.model.cycle", { name: "Vera", model: t("agents.model.opus") }) })).toBeInTheDocument();
    expect(nextModel("opus")).toBe("haiku");
  });

  it("does not switch to Opus when the warning is cancelled", async () => {
    const user = userEvent.setup();
    mount(<AgentsSettings />);
    await user.click(await screen.findByRole("button", { name: t("agents.model.cycle", { name: "Vera", model: t("agents.model.sonnet") }) }));
    const dialog = await screen.findByRole("dialog", { name: t("agents.opus.title") });
    expect(within(dialog).getByText(t("agents.opus.warning"))).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: t("agents.opus.cancel") }));
    expect(backend.sent("PUT", "/api/agents/vera/model")).toHaveLength(0);
  });

  it("turns an agent on and off", async () => {
    const user = userEvent.setup();
    mount(<AgentsSettings />);
    await user.click(await screen.findByRole("switch", { name: t("agents.toggle", { name: "Diego" }) }));
    await waitFor(() => expect(backend.sent("PUT", "/api/agents/diego/status")[0]?.body).toEqual({ status: "active" }));
    expect(await screen.findByText(t("agents.count", { active: 3, total: 3 }))).toBeInTheDocument();
  });

  it("shows the backend message when a change is refused", async () => {
    const user = userEvent.setup();
    backend.failWith = { code: "agent_file_unsafe", message: "Refused by the server", status: 409 };
    mount(<AgentsSettings />);
    await user.click(await screen.findByRole("switch", { name: t("agents.toggle", { name: "Vera" }) }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refused by the server");
  });

  it("creates my agent: validates, posts and lists it", async () => {
    const user = userEvent.setup();
    mount(<AgentsSettings />);
    await user.click(await screen.findByRole("button", { name: t("agents.create") }));
    const dialog = await screen.findByRole("dialog", { name: t("agents.create.title") });
    await user.click(within(dialog).getByRole("button", { name: t("agents.create.submit") }));
    expect(within(dialog).getAllByText(t("agents.create.required"))).toHaveLength(3);
    expect(backend.sent("POST", "/api/agents")).toHaveLength(0);

    await user.type(within(dialog).getByLabelText(t("agents.create.name")), "Chart Reader");
    await user.type(within(dialog).getByLabelText(t("agents.create.description")), "Reads charts.");
    await user.type(within(dialog).getByLabelText(t("agents.create.instructions")), "Describe the chart.");
    await user.click(within(dialog).getByRole("button", { name: t("agents.model.haiku") }));
    await user.click(within(dialog).getByRole("button", { name: t("agents.create.submit") }));

    await waitFor(() => expect(backend.sent("POST", "/api/agents")).toHaveLength(1));
    expect(backend.sent("POST", "/api/agents")[0]?.body).toEqual({
      name: "Chart Reader", description: "Reads charts.", model: "haiku", instructions: "Describe the chart.",
    });
    expect(await screen.findByText("Chart Reader")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("agents.create.another") })).toBeInTheDocument();
  });

  it("shows the real consumption and the percentage of the cap", async () => {
    mount(<ModelsSettings />);
    expect(await screen.findByText(t("models.pct", { pct: 38 }))).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: t("models.title") })).toHaveAttribute("aria-valuenow", "38");
    expect(screen.getByText(t("models.tokens", { used: (38000).toLocaleString(locale), cap: (100000).toLocaleString(locale) }))).toBeInTheDocument();
    expect(screen.getByText(t("models.note"))).toBeInTheDocument();
    expect(screen.getByText(t("models.claude.plan", { plan: "Max" }))).toBeInTheDocument();
    expect(screen.getByText(t("agents.model.sonnet"))).toBeInTheDocument();
  });

  it("asks for a cap when there is none, then saves what the owner types", async () => {
    const user = userEvent.setup();
    backend.usage.cap = null;
    backend.usage.pct = null;
    mount(<ModelsSettings />);
    expect(await screen.findByText(t("models.unset"))).toBeInTheDocument();
    await user.type(screen.getByLabelText(t("models.cap.label")), "200000");
    await user.click(screen.getByRole("button", { name: t("models.cap.save") }));
    await waitFor(() => expect(backend.sent("PUT", "/api/usage/cap")[0]?.body).toEqual({ cap: 200000 }));
    expect(await screen.findByText(t("models.pct", { pct: 19 }))).toBeInTheDocument();
  });

  it("refuses a cap that is not a positive whole number", async () => {
    const user = userEvent.setup();
    mount(<ModelsSettings />);
    const input = await screen.findByLabelText(t("models.cap.label"));
    await user.clear(input);
    await user.type(input, "abc");
    await user.click(screen.getByRole("button", { name: t("models.cap.save") }));
    expect(await screen.findByText(t("models.cap.invalid"))).toBeInTheDocument();
    expect(backend.sent("PUT", "/api/usage/cap")).toHaveLength(0);
  });

  it("removes the cap", async () => {
    const user = userEvent.setup();
    mount(<ModelsSettings />);
    await user.click(await screen.findByRole("button", { name: t("models.cap.clear") }));
    await waitFor(() => expect(backend.sent("PUT", "/api/usage/cap")[0]?.body).toEqual({ cap: null }));
    expect(await screen.findByText(t("models.unset"))).toBeInTheDocument();
  });

  it("shows the weekly percentage in the status bar, or says it is not set", async () => {
    const { unmount } = mount(<UsageStatusItem />);
    expect(await screen.findByText("38%")).toBeInTheDocument();
    unmount();
    backend.usage.cap = null;
    backend.usage.pct = null;
    mount(<UsageStatusItem />);
    expect(await screen.findByText(t("models.statusbar.unset"))).toBeInTheDocument();
  });

  it("starts with an empty list of scheduled tasks", async () => {
    mount(<TasksScreen tabKey="tasks" params={{}} active />);
    expect(await screen.findByText(t("tasks.empty.title"))).toBeInTheDocument();
    expect(screen.getByText(t("tasks.notice"))).toBeInTheDocument();
  });

  it("creates a daily task, turns it on only after the confirmation and turns it off at once", async () => {
    const user = userEvent.setup();
    mount(<TasksScreen tabKey="tasks" params={{}} active />);
    await user.click(await screen.findByRole("button", { name: t("tasks.new") }));
    const form = await screen.findByRole("dialog", { name: t("tasks.form.create") });
    await user.click(within(form).getByRole("button", { name: t("tasks.form.submit.create") }));
    expect(within(form).getAllByText(t("tasks.form.required")).length).toBeGreaterThanOrEqual(2);
    expect(backend.sent("POST", "/api/scheduled-jobs")).toHaveLength(0);

    await user.type(within(form).getByLabelText(t("tasks.form.name")), "Premarket brief");
    await user.type(within(form).getByLabelText(t("tasks.form.prompt")), "Summarize the premarket.");
    await user.click(within(form).getByRole("button", { name: t("tasks.form.submit.create") }));
    await waitFor(() => expect(backend.sent("POST", "/api/scheduled-jobs")).toHaveLength(1));
    const sent = backend.sent("POST", "/api/scheduled-jobs")[0]?.body as Record<string, string>;
    expect(sent).toMatchObject({ name: "Premarket brief", agent: "hokma", kind: "daily", expr: "07:30" });

    expect(await screen.findByText("Premarket brief")).toBeInTheDocument();
    expect(screen.getByText(t("tasks.when.daily", { time: "07:30" }))).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: t("tasks.toggle", { name: "Premarket brief" }) });
    expect(toggle).not.toBeChecked();

    await user.click(toggle);
    const confirm = await screen.findByRole("dialog", { name: t("tasks.confirm.title", { name: "Premarket brief" }) });
    expect(backend.calls.some((c) => c.path.endsWith("/activate"))).toBe(false);
    await user.click(within(confirm).getByRole("button", { name: t("tasks.confirm.no") }));
    expect(backend.calls.some((c) => c.path.endsWith("/activate"))).toBe(false);

    await user.click(toggle);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: t("tasks.confirm.yes") }));
    await waitFor(() => expect(backend.sent("POST", "/api/scheduled-jobs/1/activate")[0]?.body).toEqual({ confirm: true }));
    await waitFor(() => expect(screen.getByRole("switch", { name: t("tasks.toggle", { name: "Premarket brief" }) })).toBeChecked());

    await user.click(screen.getByRole("switch", { name: t("tasks.toggle", { name: "Premarket brief" }) }));
    await waitFor(() => expect(backend.sent("POST", "/api/scheduled-jobs/1/deactivate")).toHaveLength(1));
  });

  it("shows the last result of a task and deletes after a confirmation", async () => {
    const user = userEvent.setup();
    backend.jobs = [{
      id: 7, name: "Evening summary", agent: "hokma", agent_name: "Hokma", agent_active: true, model: "sonnet", prompt: "x",
      kind: "weekly", expr: "mon,tue,wed,thu,fri 17:00", timezone: "UTC", active: true, approved: true,
      next_run_at: "2026-11-06 17:00:00", last_run_at: "2026-11-05 17:00:00",
      last: { task_id: 3, status: "done", summary: "All quiet today.", error: null, started_at: null, ended_at: null, tokens: 17711 },
    }];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(<TasksScreen tabKey="tasks" params={{}} active />);
    expect(await screen.findByText("All quiet today.")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(t("tasks.last", { status: t("tasks.status.done") })))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(t("tasks.last.tokens", { tokens: (17711).toLocaleString(locale) }).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("tasks.delete") }));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(backend.sent("DELETE", "/api/scheduled-jobs/7")).toHaveLength(1));
    expect(await screen.findByText(t("tasks.empty.title"))).toBeInTheDocument();
    confirm.mockRestore();
  });
});

describe("schedule text", () => {
  it("describes each kind in the active language", () => {
    setLocale("en");
    expect(describeSchedule({ kind: "daily", expr: "07:30", timezone: "UTC" }, t, "en")).toBe("Every day, 07:30");
    expect(describeSchedule({ kind: "weekly", expr: "mon,tue,wed,thu,fri 07:30", timezone: "UTC" }, t, "en")).toBe("Monday - Friday, 07:30");
    expect(describeSchedule({ kind: "weekly", expr: "sat,sun 09:00", timezone: "UTC" }, t, "en")).toBe("Saturday, Sunday, 09:00");
    expect(describeSchedule({ kind: "interval", expr: "2h", timezone: "UTC" }, t, "en")).toBe("Every 2 h");
    setLocale("es");
    expect(describeSchedule({ kind: "weekly", expr: "mon,tue,wed,thu,fri 07:30", timezone: "UTC" }, t, "es")).toBe("Lunes - Viernes, 07:30");
    expect(parseWeekly("mon,fri 07:05")).toEqual({ days: ["mon", "fri"], time: "07:05" });
  });
});

describe("agentTint", () => {
  it("is deterministic per slug and always a token", async () => {
    const { agentTint } = await import("./format");
    expect(agentTint("risk")).toBe(agentTint("risk"));
    expect(agentTint("data")).toMatch(/^var\(--ag-[1-5]\)$/);
  });
});
