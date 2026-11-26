import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { FakeServer } from "../chat/testkit";
import { setHub } from "../chat/hub";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { PanelTabs } from "./PanelTabs";
import { applyStep } from "./analysisApi";
import { DeskScreen } from "./DeskScreen";
import { resetDesksStore } from "./store";
import { AnalysisTab } from "./tabs/AnalysisTab";
import type { DeskSummary } from "./types";

const DESK = {
  id: 4, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok", accounts: [],
} as unknown as DeskSummary;

const NODES = ["plan", "analysts", "company_filter", "debate", "judge", "trader", "committee", "manager"];
const steps = (states: Record<string, string>) => NODES.map((node, i) => ({ node, step: i + 1, state: states[node] ?? "pending" }));
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const AGENTS = [
  { seat: "A1", name: "Fabian", role: "analyst", step: "analysts", status: "ok", summary: "Valoracion sana", report: "Informe completo de Fabian", why: "", tools: ["market_snapshot"] },
  { seat: "A2", name: "Nadia", role: "analyst", step: "analysts", status: "absent", summary: "", report: "", why: "Sin datos de noticias", tools: [] },
];
const DONE = {
  run_id: 7, desk_id: 4, status: "done", symbol: "AAPL", as_of: "2026-11-25T10:00:00+00:00", started_at: "2026-11-25 10:00:00", ended_at: "2026-11-25 10:05:00",
  steps: steps(Object.fromEntries(NODES.map((n) => [n, "done"]))), rating: "APPROVE", grade: "B+", summary: "Entrada razonable con riesgo acotado.",
  why: ["Valoracion sana", "Nadia: Sin datos de noticias"],
  proposal: { structure: "bull_put_spread", underlying: "AAPL", expiration: "2026-12-18", contracts: 2, net_premium: 1.25, max_loss_usd: 750 },
  decision: { proposal_id: 3, decision_id: 11, status: "awaiting_owner", code: null, level: 2, strategy_key: "principal", account_id: 2, pending: true, needs_account: false },
  stopped_at: null, reason: null, detail: null, error: null, absent: ["A2"], agents: AGENTS, level: 2,
};
const brief = (over: object = {}) => ({ run_id: 7, status: "done", symbol: "AAPL", as_of: null, started_at: "2026-11-25 10:00:00", ended_at: null, rating: "APPROVE", grade: "B+", stopped_at: null, reason: null, has_proposal: true, absent: [], ...over });

interface Setup {
  list?: unknown;
  reports?: Record<number, unknown>;
  start?: Response;
  assets?: string[];
}

function stubApi(setup: Setup = {}) {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  const state = { list: setup.list ?? { desk_id: 4, available: true, reason: null, message: null, analyses: [brief()] }, reports: { 7: DONE, ...setup.reports } as Record<number, unknown> };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
      const path = String(url).split("?")[0];
      if (path === "/api/desks/4/analysis" && method === "GET") return json(state.list);
      if (path === "/api/desks/4/analysis" && method === "POST") return setup.start ?? json({ run_id: 8, symbol: "AAPL", status: "running" }, 202);
      const one = path.match(/^\/api\/desks\/4\/analysis\/(\d+)$/);
      if (one) return json(state.reports[Number(one[1])]);
      if (path.endsWith("/approve")) return json({ status: "sent", decision_id: 11 });
      if (path.endsWith("/reject")) return json({ status: "rejected", decision_id: 11 });
      if (path === "/api/desks/4") return json({ id: 4, name: "Mesa A", assets: (setup.assets ?? ["AAPL", "MSFT"]).map((symbol) => ({ symbol, note: null })), connections: [] });
      return json({ brokers: [], accounts: [] });
    }),
  );
  return { calls, state };
}

let server: FakeServer;
beforeEach(() => {
  setLocale("es");
  clearToken();
  setToken(TEST_TOKEN);
  localStorage.clear();
  server = new FakeServer();
});
afterEach(() => {
  cleanup();
  setHub(null);
  vi.unstubAllGlobals();
});

const sentWatch = () => server.sent.filter((m) => m.type === "watch_desk");

describe("Analysis panel", () => {
  it("A1: asks for the progress of the desk once, and the eight steps move as the events arrive with no polling", async () => {
    const running = { ...DONE, run_id: 8, status: "running", steps: steps({ plan: "done", analysts: "running" }), rating: null, summary: null, why: [], proposal: null, decision: null, agents: [] };
    const { calls, state } = stubApi({ list: { desk_id: 4, available: true, reason: null, message: null, analyses: [brief({ run_id: 8, status: "running", rating: null })] }, reports: { 8: running } });
    renderWithApp(<AnalysisTab desk={DESK} />);
    const list = await screen.findByRole("list", { name: t("desks.analysis.steps") });
    expect(within(list).getAllByRole("listitem")).toHaveLength(8);
    server.open();
    expect(sentWatch()).toEqual([{ type: "watch_desk", desk_id: 4 }]);
    const stateOf = (node: string) => within(list).getByText(t("desks.analysis.step." + node)).closest("li")!.getAttribute("data-state");
    expect(stateOf("plan")).toBe("done");
    expect(stateOf("analysts")).toBe("running");
    const mine = () => calls.filter((c) => c.url.startsWith("/api/desks")).length;          // the notices the hub re-reads on open (/api/orders/...) are not the panel asking again
    const before = mine();
    server.emit({ type: "analysis", event: "step", desk_id: 4, run_id: 8, symbol: "AAPL", node: "analysts", step: 2, phase: "exit", ok: true });
    server.emit({ type: "analysis", event: "step", desk_id: 4, run_id: 8, symbol: "AAPL", node: "company_filter", step: 3, phase: "enter" });
    await waitFor(() => expect(stateOf("company_filter")).toBe("running"));
    expect(stateOf("analysts")).toBe("done");
    expect(stateOf("debate")).toBe("pending");
    expect(mine()).toBe(before);                                  // pushed, not asked for again
    state.reports[8] = { ...DONE, run_id: 8 };
    server.emit({ type: "analysis", event: "finished", desk_id: 4, run_id: 8, symbol: "AAPL", status: "done", rating: "APPROVE" });
    expect(await screen.findByText("Entrada razonable con riesgo acotado.")).toBeInTheDocument();
    expect(sentWatch()).toHaveLength(1);
  });

  it("A1: asks again for the progress when the socket opens again", async () => {
    stubApi();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await screen.findByRole("list", { name: t("desks.analysis.steps") });
    server.open();
    server.drop();
    server.open();
    expect(sentWatch()).toHaveLength(2);
  });

  it("when the socket opens again the list and the open report are read again", async () => {
    const { calls } = stubApi();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await screen.findByRole("list", { name: t("desks.analysis.steps") });
    const count = (suffix: string) => calls.filter((c) => c.method === "GET" && c.url.split("?")[0] === `/api/desks/4/analysis${suffix}`).length;
    server.open();
    server.drop();
    const lists = count(""), reports = count("/7");
    server.open();
    await waitFor(() => expect(count("")).toBe(lists + 1));
    await waitFor(() => expect(count("/7")).toBe(reports + 1));
  });

  it("A2 and A3: the report shows grade, note, reasons, proposal and one fold per agent, and an absent agent says so with its cause", async () => {
    stubApi();
    renderWithApp(<AnalysisTab desk={DESK} />);
    expect(await screen.findByText("Entrada razonable con riesgo acotado.")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: t("desks.analysis.verdict") })).getByText("APPROVE")).toBeInTheDocument();
    const why = screen.getByRole("heading", { name: t("desks.analysis.why") }).nextElementSibling as HTMLElement;
    expect(within(why).getAllByRole("listitem")).toHaveLength(2);
    const proposal = within(screen.getByRole("region", { name: t("desks.analysis.proposal") }));
    expect(proposal.getByText("bull_put_spread")).toBeInTheDocument();
    const agents = within(screen.getByRole("region", { name: t("desks.analysis.agents") }));
    const folds = agents.getAllByRole("group");
    expect(folds).toHaveLength(2);
    expect(within(folds[0]).getByText("Informe completo de Fabian")).toBeInTheDocument();
    expect(within(folds[0]).getByText(/market_snapshot/)).toBeInTheDocument();
    expect(within(folds[1]).getByText(t("desks.analysis.agent.absent"))).toBeInTheDocument();
    expect(within(folds[1]).getByText("Sin datos de noticias")).toBeInTheDocument();
    expect(within(folds[1]).queryByText(t("desks.analysis.agent.why"))).toBeNull();
  });

  it("A4: Take the trade approves the decision of the proposal and says why when it did not go out", async () => {
    const { calls } = stubApi();
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.take") }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/desks/4/orders/pending/11/approve" && c.method === "POST")).toBe(true));
    expect(await screen.findByText(t("desks.analysis.decision.sent"))).toBeInTheDocument();
  });

  it("A4: a rule that no longer holds shows its reason", async () => {
    stubApi();
    vi.mocked(fetch).mockImplementation(async (url: unknown, init?: RequestInit) => {
      const path = String(url).split("?")[0];
      if (path.endsWith("/approve")) return json({ status: "not_sent", decision_id: 11, why: "Regla Maximo 2 posiciones" });
      if (path === "/api/desks/4/analysis") return json({ desk_id: 4, available: true, reason: null, message: null, analyses: [brief()] });
      if (path === "/api/desks/4/analysis/7") return json(DONE);
      if (path === "/api/desks/4") return json({ id: 4, name: "Mesa A", assets: [{ symbol: "AAPL", note: null }], connections: [] });
      void init;
      return json({});
    });
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.take") }));
    expect(await screen.findByText(/Regla Maximo 2 posiciones/)).toBeInTheDocument();
  });

  it("A4: No tomarla rejects the decision", async () => {
    const { calls } = stubApi();
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.skip") }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/desks/4/orders/pending/11/reject" && c.method === "POST")).toBe(true));
  });

  it("a decision that needs an account opens Add account", async () => {
    stubApi({ reports: { 7: { ...DONE, decision: { ...DONE.decision, pending: false, needs_account: true, code: "needs_account", account_id: null } } } });
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    expect(screen.queryByRole("button", { name: t("desks.analysis.take") })).toBeNull();
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.addAccount") }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("without a proposal the bar says where it stopped and offers another asset", async () => {
    stubApi({ reports: { 7: { ...DONE, decision: null, proposal: null, stopped_at: "judge", detail: "El juez no vio ventaja", level: 1 } } });
    renderWithApp(<AnalysisTab desk={DESK} />);
    const bar = within(await screen.findByRole("region", { name: t("desks.analysis.decision") }));
    expect(bar.getByText(/El juez no vio ventaja/)).toBeInTheDocument();
    expect(bar.getByText(/nivel 1/)).toBeInTheDocument();
    expect(bar.getByRole("button", { name: t("desks.analysis.other") })).toBeInTheDocument();
  });

  it("409: a second analysis of the same asset opens the one that goes", async () => {
    const running = { ...DONE, run_id: 9, status: "running", steps: steps({ plan: "running" }), rating: null, summary: null, why: [], proposal: null, decision: null, agents: [] };
    const { calls } = stubApi({ start: json({ code: "analysis_running", message: "ya hay uno", run_id: 9 }, 409), reports: { 9: running } });
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.analyze") }));
    expect(await screen.findByText(t("desks.analysis.alreadyRunning", { symbol: "AAPL" }))).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.url === "/api/desks/4/analysis/9")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ symbol: "AAPL" });
  });

  it("409 busy and over budget show the message of the server and open nothing", async () => {
    stubApi({ start: json({ code: "desk_budget_exhausted", message: "La mesa agoto su presupuesto de la semana" }, 409) });
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    await user.click(await screen.findByRole("button", { name: t("desks.analysis.analyze") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("La mesa agoto su presupuesto de la semana");
  });

  it("A6: the previous analyses list and open on a click", async () => {
    const older = { ...DONE, run_id: 5, symbol: "MSFT", summary: "Otra nota" };
    stubApi({ list: { desk_id: 4, available: true, reason: null, message: null, analyses: [brief(), brief({ run_id: 5, symbol: "MSFT" })] }, reports: { 5: older } });
    const user = userEvent.setup();
    renderWithApp(<AnalysisTab desk={DESK} />);
    const prev = within(await screen.findByRole("region", { name: t("desks.analysis.previous") }));
    await user.click(prev.getByText("MSFT"));
    expect(await screen.findByText("Otra nota")).toBeInTheDocument();
  });

  it("A8: a desk that is not an options desk shows the reason and no controls", async () => {
    stubApi({ list: { desk_id: 4, available: false, reason: "not_options_desk", message: "Esta mesa no opera opciones sobre acciones", analyses: [] } });
    renderWithApp(<AnalysisTab desk={DESK} />);
    expect(await screen.findByText("Esta mesa no opera opciones sobre acciones")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("desks.analysis.analyze") })).toBeNull();
  });

  it("A8: the Add menu lists Analysis disabled with its reason", async () => {
    const user = userEvent.setup();
    renderWithApp(
      <PanelTabs order={["terminal", "rules", "agents", "config"]} tab="terminal" tabKey="k" onTab={() => undefined} onOrder={() => undefined} onAdd={() => undefined} onRemove={() => undefined}
        blocked={{ analysis: "Esta mesa no opera opciones sobre acciones" }} />,
    );
    await user.click(screen.getByRole("button", { name: t("desks.panels.add") }));
    const item = screen.getByRole("button", { name: new RegExp(t("desks.tab.analysis")) });
    expect(item).toBeDisabled();
    expect(item).toHaveTextContent("Esta mesa no opera opciones sobre acciones");
  });

  it("A7: a run the chief opens from the chat adds the Analysis panel and opens it, live", async () => {
    resetDesksStore();
    const { calls } = stubApi();
    vi.mocked(fetch).mockImplementation(async (url: unknown, init?: RequestInit) => {
      const path = String(url).split("?")[0];
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (path === "/api/desks/4/panels") return json({ panels: ["terminal", "analysis", "agents", "operations", "rules", "config"] });
      if (path === "/api/desks/4/analysis") return json({ desk_id: 4, available: true, reason: null, message: null, analyses: [brief({ run_id: 8, status: "running", rating: null })] });
      if (path === "/api/desks/4/analysis/8") return json({ ...DONE, run_id: 8, status: "running", steps: steps({ plan: "running" }), rating: null, summary: null, why: [], proposal: null, decision: null, agents: [] });
      if (path === "/api/desks/4") return json({ id: 4, name: "Mesa A", assets: [{ symbol: "AAPL", note: null }], connections: [] });
      return json({ desks: [] });
    });
    renderWithApp(<DeskScreen desk={{ ...DESK, panels: ["terminal", "agents", "operations", "rules", "config"] }} desks={[DESK]} tabKey="k" />);
    await waitFor(() => expect(server.hub.status).toBeDefined());
    server.open();
    await waitFor(() => expect(sentWatch().length).toBeGreaterThan(0));
    server.emit({ type: "analysis", event: "started", desk_id: 4, run_id: 8, symbol: "MSFT", steps: NODES });
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/desks/4/panels" && JSON.stringify(c.body) === JSON.stringify({ id: "analysis" }))).toBe(true));
    expect(await screen.findByRole("tab", { name: t("desks.tab.analysis"), selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: t("desks.analysis.steps") })).toBeInTheDocument();
  });

  it("applyStep: enter runs the step, a failed exit stops it, other events change nothing", () => {
    const base = steps({ plan: "done" }) as ReturnType<typeof steps> & never;
    const ev = { type: "analysis", desk_id: 4, run_id: 1 } as const;
    expect(applyStep(base as never, { ...ev, event: "step", node: "analysts", phase: "enter" })[1].state).toBe("running");
    expect(applyStep(base as never, { ...ev, event: "step", node: "analysts", phase: "exit", ok: false })[1].state).toBe("stopped");
    expect(applyStep(base as never, { ...ev, event: "finished" })).toBe(base);
  });
});
