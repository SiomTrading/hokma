import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { DesksScreen } from "./DesksScreen";
import { resetDesksStore } from "./store";
import type { DeskSummary } from "./types";

/** The long dash, built by code so this file stays free of it. */
const DASH = String.fromCharCode(0x2014);

function desk(patch: Partial<DeskSummary> = {}): DeskSummary {
  return {
    id: 7, project_id: 10, name: "Futuros NQ", focus: "Intraday futures", capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 3, agents_working: 1, tokens_week: 0, token_budget: 300000, budget_state: "ok", ...patch,
  };
}

interface Member {
  slug: string;
  name: string;
  role: string;
  model: string;
  active: boolean;
  state: string;
  tokens_week: number;
  last_action: string | null;
  can?: string[];
  cannot?: string[];
  activity?: Array<{ at: string; text: string }>;
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

interface Server {
  calls: Call[];
  members: Member[];
  usage: Record<string, unknown>;
  catalog: Array<Record<string, unknown>>;
  addFails: string | null;
  failLoad: boolean;
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const member = (slug: string, name: string, role: string, patch: Partial<Member> = {}): Member => ({
  slug, name, role, model: "sonnet", active: true, state: "idle", tokens_week: 0, last_action: null, ...patch,
});

const agentItem = (slug: string, name: string, patch: Record<string, unknown> = {}) => ({
  slug, name, description: `${name} description`, tier: "balanced", model: "sonnet", status: "active", active: true,
  is_custom: false, is_orchestrator: false, enabled_session: null, role: "specialist", ...patch,
});

function stubServer(over: Partial<Server> = {}): Server {
  const server: Server = {
    calls: [],
    members: [
      member("desk-chief", "Jefe de mesa", "chief", { state: "working", tokens_week: 60000, last_action: "Propuso entrada NQ en el VWAP", can: ["desks.agents.can.chief"], cannot: ["desks.agents.cannot.chief"], activity: [{ at: "10:42", text: "Propuso entrada NQ en el VWAP" }, { at: "10:40", text: "Received the mandate" }] }),
      member("hugo", "Hugo", "data", { model: "haiku", tokens_week: 20000 }),
      member("mi-analista", "Mi analista", "custom", { active: false, state: "off", can: ["Revisa opciones"], cannot: [] }),
    ],
    usage: { token_budget: 300000, used: 80000, pct: 27, state: "ok" },
    catalog: [agentItem("iris", "Iris"), agentItem("diego", "Diego", { active: false, status: "off", model: "haiku" }), agentItem("hugo", "Hugo")],
    addFails: null,
    failLoad: false,
    ...over,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      const path = String(url);
      server.calls.push({ method, url: path, body });
      if (path === "/api/desks") return json({ desks: [desk()] });
      if (path === "/api/desks/7/agents" && method === "GET") {
        return server.failLoad ? json({ detail: { code: "x", message: "boom" } }, 500) : json({ agents: server.members });
      }
      if (path === "/api/desks/7/usage") return json(server.usage);
      if (path === "/api/agents" && method === "GET") return json({ orchestrator: null, agents: server.catalog, active: 2, total: 3 });
      if (path === "/api/agents" && method === "POST") return json(agentItem("nuevo", "Nuevo", { is_custom: true, model: (body as { model: string }).model }));
      if (path.startsWith("/api/agents/") && path.endsWith("/status")) return json({});
      if (path === "/api/desks/7/agents" && method === "POST") {
        if (server.addFails) return json({ detail: { code: server.addFails, message: "refused" } }, 409);
        const b = body as { agent_slug: string; model: string };
        server.members.push(member(b.agent_slug, b.agent_slug, "custom", { model: b.model }));
        return json({ ok: true }, 201);
      }
      if (path.startsWith("/api/desks/7/agents/") && method === "PUT") {
        const slug = decodeURIComponent(path.split("/").pop() ?? "");
        const found = server.members.find((m) => m.slug === slug);
        if (found) Object.assign(found, body);
        return json({ ok: true });
      }
      if (path.startsWith("/api/desks/7/agents/") && method === "DELETE") {
        const slug = decodeURIComponent(path.split("/").pop() ?? "");
        server.members = server.members.filter((m) => m.slug !== slug);
        return json({ ok: true });
      }
      return json({});
    }),
  );
  return server;
}

const sent = (server: Server, method: string, url: string) => server.calls.filter((c) => c.method === method && c.url === url);

function open() {
  setToken(TEST_TOKEN);
  return renderWithApp(<DesksScreen tabKey="desks:7" params={{ desk: "7", tab: "agents" }} active />);
}

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("team of the desk", () => {
  it("groups the agents by state, with the header numbers and the detail of the first one", async () => {
    stubServer();
    open();
    expect(await screen.findByText(t("desks.agents.working"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.agents.workingOf", { total: 3 }))).toBeInTheDocument();
    const groups = screen.getAllByRole("group").filter((g) => g.getAttribute("aria-label")?.match(/^(Trabajando|En espera|Apagados)$/));
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["Trabajando", "En espera", "Apagados"]);
    expect(within(groups[0]!).getByText("Jefe de mesa")).toBeInTheDocument();
    expect(within(groups[1]!).getByText("Hugo")).toBeInTheDocument();
    expect(within(groups[2]!).getByText("Mi analista")).toBeInTheDocument();
    expect(screen.getByText("80.000")).toBeInTheDocument();
    // Detail of the first agent: model on this desk, Can and Cannot from the server's keys, tokens and activity.
    expect(screen.getByRole("button", { name: "Sonnet", pressed: true })).toBeInTheDocument();
    expect(screen.getByText(t("desks.agents.can.chief"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.agents.cannot.chief"))).toBeInTheDocument();
    expect(screen.getByText("Received the mandate")).toBeInTheDocument();
    expect(screen.getAllByText("60.000")).toHaveLength(2);
  });

  it("shows no money anywhere", async () => {
    stubServer();
    const { container } = open();
    await screen.findByText(t("desks.agents.working"));
    expect(container.textContent ?? "").not.toMatch(/US\$|\$\s?\d|USD/);
  });

  it("shows the text of a student's own agent as written and the default for what it cannot do", async () => {
    stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.select", { name: "Mi analista" }) }));
    expect(screen.getByText("Revisa opciones")).toBeInTheDocument();
    expect(screen.getByText(t("desks.agents.cannot.custom"))).toBeInTheDocument();
  });

  it("falls back to the role's text when the server sends none", async () => {
    stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.select", { name: "Hugo" }) }));
    expect(screen.getByText("Trae datos y calcula resultado y drawdown.")).toBeInTheDocument();
    expect(screen.getByText("No decide operaciones.")).toBeInTheDocument();
  });

  it("says when the team cannot be loaded and retries", async () => {
    const server = stubServer({ failLoad: true });
    open();
    expect(await screen.findByText("boom")).toBeInTheDocument();
    server.failLoad = false;
    await userEvent.click(screen.getByRole("button", { name: /reintentar|intentar/i }));
    expect(await screen.findByText(t("desks.agents.working"))).toBeInTheDocument();
  });
});

describe("model per desk", () => {
  it("changing it calls only this desk and leaves the global agent alone", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: "Haiku" }));
    await waitFor(() => expect(sent(server, "PUT", "/api/desks/7/agents/desk-chief")).toHaveLength(1));
    expect(sent(server, "PUT", "/api/desks/7/agents/desk-chief")[0]!.body).toEqual({ model: "haiku" });
    expect(server.calls.some((c) => c.url.startsWith("/api/agents/") && c.method === "PUT")).toBe(false);
    expect(await screen.findByRole("button", { name: "Haiku", pressed: true })).toBeInTheDocument();
  });

  it("asks before Opus: cancel changes nothing, confirm sends it", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: "Opus" }));
    await userEvent.click(screen.getByRole("button", { name: t("agents.opus.cancel") }));
    expect(sent(server, "PUT", "/api/desks/7/agents/desk-chief")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Opus" }));
    await userEvent.click(screen.getByRole("button", { name: t("agents.opus.confirm") }));
    await waitFor(() => expect(sent(server, "PUT", "/api/desks/7/agents/desk-chief")[0]?.body).toEqual({ model: "opus" }));
  });

  it("turning an agent off in the desk sends active false", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.select", { name: "Hugo" }) }));
    await userEvent.click(screen.getByRole("switch", { name: t("desks.agents.active") }));
    await waitFor(() => expect(sent(server, "PUT", "/api/desks/7/agents/hugo")[0]?.body).toEqual({ active: false }));
  });
});

describe("budget meter", () => {
  it("shows used against the cap and the rule, with no warning under 80%", async () => {
    stubServer();
    open();
    expect(await screen.findByText("80.000 / 300.000")).toBeInTheDocument();
    expect(screen.getByText(t("desks.agents.budget.rule"))).toBeInTheDocument();
    expect(screen.queryByText(t("desks.agents.budget.warn"))).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: t("desks.agents.budget.label") })).toHaveAttribute("aria-valuenow", "27");
  });

  it("warns in amber from 80%", async () => {
    stubServer({ usage: { token_budget: 300000, used: 250000, pct: 83, state: "warn" } });
    const { container } = open();
    expect(await screen.findByText(t("desks.agents.budget.warn"))).toBeInTheDocument();
    expect(container.querySelector(".meter.warn")).not.toBeNull();
  });

  it("at 100% says the desk stops and why", async () => {
    stubServer({ usage: { token_budget: 300000, used: 310000, pct: 100, state: "exhausted" } });
    const { container } = open();
    expect(await screen.findByRole("alert")).toHaveTextContent(t("desks.agents.budget.exhausted"));
    expect(container.querySelector(".meter.bad")).not.toBeNull();
  });

  it("with no cap there is no meter, only the words", async () => {
    stubServer({ usage: { token_budget: null, used: 5000, pct: null, state: "ok" } });
    open();
    expect(await screen.findByText(t("desks.agents.budget.none"))).toBeInTheDocument();
    expect(screen.queryByRole("progressbar", { name: t("desks.agents.budget.label") })).not.toBeInTheDocument();
  });
});

describe("bring an existing agent", () => {
  const rowOf = (dialog: HTMLElement, name: string) => within(dialog).getByRole("button", { name: t("desks.agents.bring.addNamed", { name }) });

  it("lists the agents with a search, marks those already in the desk and adds one with its own model", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.bring") }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Iris")).toBeInTheDocument();
    expect(within(dialog).getByText(t("desks.agents.bring.inDesk"))).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: t("desks.agents.bring.addNamed", { name: "Hugo" }) })).not.toBeInTheDocument();
    await userEvent.click(rowOf(dialog, "Iris"));
    await waitFor(() => expect(sent(server, "POST", "/api/desks/7/agents")).toHaveLength(1));
    expect(sent(server, "POST", "/api/desks/7/agents")[0]!.body).toEqual({ agent_slug: "iris", model: "sonnet" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("filters live by name, role and description, ignoring accents, and says when nothing matches", async () => {
    stubServer({ catalog: [agentItem("iris", "Iris", { description: "Investigaci\u00f3n de mercado" }), agentItem("diego", "Diego", { is_custom: true })] });
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.bring") }));
    const dialog = await screen.findByRole("dialog");
    const box = await within(dialog).findByRole("searchbox", { name: t("desks.agents.bring.search") });
    await userEvent.type(box, "investigacion");
    expect(within(dialog).getByText("Iris")).toBeInTheDocument();
    expect(within(dialog).queryByText("Diego")).not.toBeInTheDocument();
    await userEvent.clear(box);
    await userEvent.type(box, "personalizado");
    expect(within(dialog).getByText("Diego")).toBeInTheDocument();
    expect(within(dialog).queryByText("Iris")).not.toBeInTheDocument();
    await userEvent.clear(box);
    await userEvent.type(box, "zzz");
    expect(within(dialog).getByText(t("desks.agents.bring.none"))).toBeInTheDocument();
  });

  it("an agent that is off is turned on and added in the same click", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.bring") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("button", { name: t("desks.agents.bring.addNamed", { name: "Diego" }) }));
    await waitFor(() => expect(sent(server, "PUT", "/api/agents/diego/status")[0]?.body).toEqual({ status: "active" }));
    await waitFor(() => expect(sent(server, "POST", "/api/desks/7/agents")).toHaveLength(1));
  });

  it("asks before adding an agent that runs on Opus", async () => {
    const server = stubServer({ catalog: [agentItem("iris", "Iris", { model: "opus" })] });
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.bring") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("button", { name: t("desks.agents.bring.addNamed", { name: "Iris" }) }));
    expect(sent(server, "POST", "/api/desks/7/agents")).toHaveLength(0);
    expect(screen.getAllByRole("dialog").length).toBeGreaterThan(1);
  });

  it("shows the server's reason when adding fails", async () => {
    stubServer({ addFails: "duplicate" });
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.bring") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("button", { name: t("desks.agents.bring.addNamed", { name: "Iris" }) }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("refused");
  });
});

describe("create an agent from the desk", () => {
  it("creates it with the existing flow and then links it to this desk", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.create") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(t("agents.create.name")), "Nuevo");
    await userEvent.type(within(dialog).getByLabelText(t("agents.create.description")), "Hace algo");
    await userEvent.type(within(dialog).getByLabelText(t("agents.create.instructions")), "Haz algo");
    await userEvent.click(within(dialog).getByRole("button", { name: t("agents.create.submit") }));
    await waitFor(() => expect(sent(server, "POST", "/api/desks/7/agents")).toHaveLength(1));
    expect(sent(server, "POST", "/api/agents")).toHaveLength(1);
    expect(sent(server, "POST", "/api/desks/7/agents")[0]!.body).toEqual({ agent_slug: "nuevo", model: "sonnet", role: "custom" });
  });
});

describe("remove from the desk", () => {
  it("asks first, then removes only the link", async () => {
    const server = stubServer();
    open();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.agents.select", { name: "Hugo" }) }));
    await userEvent.click(screen.getByRole("button", { name: t("desks.agents.remove") }));
    expect(screen.getByText(t("desks.agents.remove.ask", { name: "Hugo" }))).toBeInTheDocument();
    expect(sent(server, "DELETE", "/api/desks/7/agents/hugo")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: t("desks.agents.remove.confirm") }));
    await waitFor(() => expect(sent(server, "DELETE", "/api/desks/7/agents/hugo")).toHaveLength(1));
    expect(server.calls.some((c) => c.method === "DELETE" && c.url.startsWith("/api/agents"))).toBe(false);
    await waitFor(() => expect(screen.queryByRole("button", { name: t("desks.agents.select", { name: "Hugo" }) })).not.toBeInTheDocument());
  });

  it("the chief cannot be removed", async () => {
    stubServer();
    open();
    expect(await screen.findByRole("button", { name: t("desks.agents.remove") })).toBeDisabled();
  });
});

describe("languages", () => {
  it("every Spanish key has its English text and none carries a long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("desks.agents."));
    expect(keys.length).toBeGreaterThan(60);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key]).not.toContain(DASH);
      expect(en[key]).not.toContain(DASH);
    }
  });

  it("shows the kit agents with Spanish names in es and the English ones in en, whatever name the server sends", async () => {
    const english = [
      member("desk-chief", "Desk Chief", "chief", { state: "working" }),
      member("bear-case", "Bear Case", "bear"),
      member("bull-case", "Bull Case", "bull"),
      member("judge", "Judge", "judge"),
      member("risk", "Risk", "risk"),
      member("signals", "Signals", "signals"),
    ];
    stubServer({ members: english });
    open();
    await screen.findByText(t("desks.agents.working"));
    const list = () => screen.getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? "");
    for (const name of ["Jefe de mesa", "En contra", "A favor", "Juez", "Riesgo", t("desks.role.signals")]) {
      expect(screen.getAllByText(name).length, name).toBeGreaterThan(0);
    }
    expect(list().join("|")).not.toMatch(/Bear Case|Bull Case|Desk Chief|Judge/);
    for (const name of ["Bear Case", "Bull Case", "Desk Chief", "Judge", "Signals"]) expect(screen.queryByText(name)).not.toBeInTheDocument();
    cleanup();
    setLocale("en");
    stubServer({ members: english });
    open();
    await screen.findByText("Working now");
    for (const name of ["Desk Chief", "Bear Case", "Bull Case", "Judge", "Risk", "Signals"]) {
      expect(screen.getAllByText(name).length, name).toBeGreaterThan(0);
    }
  });

  it("reads in English", async () => {
    stubServer();
    setLocale("en");
    open();
    expect(await screen.findByText("Working now")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add agent" })).toBeInTheDocument();
    expect(screen.getByText("Talks with you, hands out the work and writes the decision.")).toBeInTheDocument();
  });
});
