import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { t } from "@/i18n";
import { FakeServer } from "@/screens/chat/testkit";
import { setHub } from "@/screens/chat/hub";
import { WorkingLine } from "@/screens/chat/WorkingLine";
import { useActivity } from "@/screens/chat/useActivity";
import { MessageItem } from "@/screens/chat/Messages";
import { followConversation, resetPanelState } from "@/shell/panelState";
import { RightPanel } from "@/shell/RightPanel";
import { LAYOUT_KEY } from "@/shell/tabsStorage";
import { renderShell, renderWithApp, TEST_TOKEN } from "@/test/utils";
import { resetAgentNames } from "./agentNames";
import { AgentsSection } from "./AgentsSection";
import { BackgroundFeed } from "./feed";
import { BackgroundSection } from "./BackgroundSection";
import { ResultsSection } from "./ResultsSection";

const AGENTS = { orchestrator: null, agents: [{ slug: "iris", name: "Iris" }, { slug: "hugo", name: "Hugo" }], active: 2, total: 2 };
let payloads: Record<string, unknown>;
let fetched: string[];
let server: FakeServer;
const base = { conversation_id: "7", turn_id: "t1" };
const step = (id: string, over: Record<string, unknown> = {}) => ({ id, name: "Read", kind: "read_file", target: "data/prices.csv", is_error: null, ...over });
const agentStart = (id: string, slug: string, over: Record<string, unknown> = {}) =>
  server.emit({ type: "agent_start", ...base, agent_id: id, slug, description: `Encargo de ${slug}`, parent: "", started_at: Date.now(), ...over });

function startTurn(text = "Investiga el NQ") {
  server.hub.send({ type: "send", conversation_id: "7", text });
  server.emit({ type: "conversation", conversation_id: "7", title: "Plan", model: "sonnet", agent: null, project: null, created: false });
  server.emit({ type: "turn_start", ...base, message_id: "1" });
}

beforeEach(() => {
  setToken(TEST_TOKEN);
  localStorage.clear();
  resetPanelState();
  resetAgentNames();
  fetched = [];
  payloads = {
    "/api/agents": AGENTS,
    "/api/scheduled-jobs": { jobs: [], running: [] },
    "/api/backup": { status: "idle", last_backup_at: null, message: null },
    "/api/runs/active": { running: [], finished_24h: [] },
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    fetched.push(url);
    const key = Object.keys(payloads).find((k) => url.startsWith(k));
    return new Response(JSON.stringify(key ? payloads[key] : {}));
  }));
  server = new FakeServer();
  server.open();
  followConversation({ conversationId: "7", source: "chat", label: "Plan" });
});
afterEach(() => {
  setHub(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Agents in this session", () => {
  it("A1 A4: an agent appears working with its step and clock, ends with its result and its steps", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    agentStart("a1", "iris");
    server.emit({ type: "agent_step", ...base, agent_id: "a1", step: step("s1") });
    const row = await screen.findByText("Iris");
    const line = row.closest('[data-testid="agent-row"]') as HTMLElement;
    expect(line).toHaveAttribute("data-status", "working");
    expect(line).toHaveTextContent(`${t("chat.tool.read_file")} data/prices.csv`);
    expect(line).toHaveTextContent("Encargo de iris");
    expect(screen.getByTestId("turn-row")).toHaveTextContent("Investiga el NQ");
    server.emit({ type: "agent_step", ...base, agent_id: "a1", step: step("s1", { is_error: false }) });
    server.emit({ type: "agent_end", ...base, agent_id: "a1", status: "done", ended_at: Date.now(), result: "## Hallazgos\n\nTodo bien" });
    expect(line).toHaveAttribute("data-status", "done");
    await userEvent.click(within(line).getByRole("button", { name: t("panel.agents.viewResult") }));
    expect(within(line).getByRole("heading", { name: "Hallazgos" })).toBeInTheDocument();
    expect(within(line).getByText(t("panel.agents.steps", { n: 1 }))).toBeInTheDocument();
  });

  it("A3: an agent that fails shows its reason, and a stop shows the stopped notice", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    agentStart("a1", "iris");
    agentStart("a2", "hugo");
    server.emit({ type: "agent_end", ...base, agent_id: "a1", status: "error", ended_at: Date.now(), result: "No access to that file" });
    server.emit({ type: "agent_end", ...base, agent_id: "a2", status: "stopped", ended_at: Date.now(), result: "" });
    const [iris, hugo] = screen.getAllByTestId("agent-row");
    expect(iris).toHaveAttribute("data-status", "error");
    await userEvent.click(within(iris!).getByRole("button", { name: t("panel.agents.viewReason") }));
    expect(iris).toHaveTextContent("No access to that file");
    await userEvent.click(within(hugo!).getByRole("button", { name: t("panel.agents.viewReason") }));
    expect(hugo).toHaveTextContent(t("panel.agents.stoppedMsg"));
  });

  it("A8: an agent that launches another hangs from its parent, indented", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    agentStart("a1", "iris");
    agentStart("a2", "hugo", { parent: "a1" });
    const [first, second] = screen.getAllByTestId("agent-row");
    expect(first).toHaveStyle({ paddingLeft: "16px" });
    expect(second).toHaveStyle({ paddingLeft: "28px" });
  });

  it("the turn waits for a permission and says so, in the panel and in the chat line (A9, T5)", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    agentStart("a1", "iris");
    server.emit({ type: "permission_request", ...base, request_id: "r1", tool_use_id: "s1", tool: "Read", input: {}, summary: { kind: "read_file", target: "a" }, expires_at: 1, agent: { id: "a1", slug: "iris" } });
    expect(screen.getByTestId("agent-row")).toHaveAttribute("data-status", "waiting");
    expect(screen.getByTestId("agent-row")).toHaveTextContent(t("panel.agents.waiting"));
    server.emit({ type: "permission_resolved", conversation_id: "7", request_id: "r1", decision: "allow", by: "human" });
    expect(screen.getByTestId("agent-row")).toHaveAttribute("data-status", "working");
  });

  it("T2 A7: the panel follows the conversation that has the focus and keeps the other one alive", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    agentStart("a1", "iris");
    expect(await screen.findByText("Iris")).toBeInTheDocument();
    server.emit({ type: "turn_start", conversation_id: "8", turn_id: "u1", message_id: "2" });
    server.emit({ type: "agent_start", conversation_id: "8", turn_id: "u1", agent_id: "b1", slug: "hugo", description: "Mesa", parent: "", started_at: Date.now() });
    act(() => followConversation({ conversationId: "8", source: "desk", label: "Futuros" }));
    expect(await screen.findByText("Hugo")).toBeInTheDocument();
    expect(screen.queryByText("Iris")).toBeNull();
    expect(screen.getByText(t("rightPanel.deskConversation", { name: "Futuros" }))).toBeInTheDocument();
    act(() => followConversation({ conversationId: "7", source: "chat", label: "Plan" }));
    expect(await screen.findByText("Iris")).toBeInTheDocument();
    expect(screen.getByTestId("agent-row")).toHaveAttribute("data-status", "working");
  });

  it("says there is nothing to see when no conversation is open", () => {
    resetPanelState();
    renderWithApp(<AgentsSection />);
    expect(screen.getByText(t("panel.agents.none"))).toBeInTheDocument();
  });
});

describe("Results", () => {
  it("R1: one row per file, who made it and whether it was created or edited", async () => {
    renderWithApp(<ResultsSection />);
    startTurn();
    server.emit({ type: "tool_use", ...base, tool_use_id: "w1", name: "Write", input: {}, summary: { kind: "write_file", target: "/p/notes/a.md" } });
    server.emit({ type: "tool_result", ...base, tool_use_id: "w1", is_error: false, text: "" });
    agentStart("a1", "iris");
    server.emit({ type: "agent_step", ...base, agent_id: "a1", step: step("e1", { name: "Edit", kind: "edit_file", target: "/p/b.md", is_error: false }) });
    server.emit({ type: "tool_use", ...base, tool_use_id: "e2", name: "Edit", input: {}, summary: { kind: "edit_file", target: "/p/notes/a.md" } });
    server.emit({ type: "tool_result", ...base, tool_use_id: "e2", is_error: false, text: "" });
    server.emit({ type: "tool_use", ...base, tool_use_id: "w2", name: "Write", input: {}, summary: { kind: "write_file", target: "/p/c.md" } });
    server.emit({ type: "tool_result", ...base, tool_use_id: "w2", is_error: true, text: "no" });
    const rows = await screen.findAllByTestId("result-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("a.md");
    expect(rows[0]).toHaveTextContent(t("panel.results.created"));
    expect(rows[0]).toHaveTextContent(t("panel.agents.main"));
    expect(rows[1]).toHaveTextContent(t("panel.results.modified"));
    await waitFor(() => expect(rows[1]).toHaveTextContent("Iris"));
  });
});

describe("The chat line (T5)", () => {
  it("says who works, who waits and how many, with the link to the panel, and goes away with the turn", async () => {
    const Live = () => <WorkingLine conversationId="7" running={useActivity("7").running} />;
    renderWithApp(<Live />);
    startTurn();
    expect(screen.getByTestId("working-line")).toHaveTextContent(`${t("chat.working.main")}, ${t("chat.working.link")}`);
    agentStart("a1", "iris");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(`${t("chat.working.one", { name: "Iris" })}, ${t("chat.working.link")}`));
    agentStart("a2", "hugo");
    expect(screen.getByRole("status")).toHaveTextContent(t("chat.working.many", { n: 2 }));
    server.emit({ type: "permission_request", ...base, request_id: "r1", tool_use_id: "s1", tool: "Read", input: {}, summary: { kind: "read_file", target: "a" }, expires_at: 1, agent: { id: "a1", slug: "iris" } });
    expect(screen.getByRole("status")).toHaveTextContent(t("chat.working.waiting", { name: "Iris" }));
    expect(screen.queryByRole("button", { name: t("chat.working.link") })).toBeNull();
    server.emit({ type: "done", ...base, message_id: null, stopped: false });
    expect(screen.queryByTestId("working-line")).toBeNull();
  });

  it("A9: the permission card names the agent that asks", async () => {
    renderWithApp(
      <MessageItem
        item={{ kind: "permission", key: "p", body: { turn_id: "t1", request_id: "r", tool_use_id: "s", tool: "Read", input: {}, summary: { kind: "read_file", target: "a" }, expires_at: 1, agent: { id: "a1", slug: "iris" } } }}
        actions={{ projectName: "", canSave: false, onPermission: () => undefined, onSaveMessage: () => undefined, onSaveAttachment: () => undefined }}
      />,
    );
    expect(await screen.findByText(t("chat.perm.agent.badge", { name: "Iris" }))).toBeInTheDocument();
  });
});

describe("In the background", () => {
  it("B1: a routine in course shows its name, its agent and a clock, and passes to the finished ones when it ends", async () => {
    const job = (last: Record<string, unknown>) => ({ id: 5, name: "Resumen de apertura", agent: "iris", agent_name: "Iris", last });
    payloads["/api/scheduled-jobs"] = { jobs: [job({ task_id: 1, status: "running", summary: "", started_at: new Date(Date.now() - 65_000).toISOString() })], running: [5] };
    renderWithApp(
      <BackgroundFeed open>
        <BackgroundSection />
      </BackgroundFeed>,
    );
    const row = await screen.findByTestId("bg-row");
    expect(row).toHaveTextContent("Resumen de apertura");
    expect(row).toHaveTextContent("Iris");
    expect(row).toHaveTextContent("1m");
    expect(screen.queryByText(t("panel.bg.finished", { n: 1 }))).toBeNull();
  });

  it("B2 T6: the backup and a desk cycle in course show up; when they end they count as finished", async () => {
    payloads["/api/backup"] = { status: "running", last_backup_at: null, message: null };
    payloads["/api/runs/active"] = { running: [{ run_id: 3, desk_id: 1, desk_name: "Futuros", kind: "cycle", started_at: "2026-11-24T10:00:00Z" }], finished_24h: [] };
    renderWithApp(
      <BackgroundFeed open>
        <BackgroundSection />
      </BackgroundFeed>,
    );
    const rows = await screen.findAllByTestId("bg-row");
    expect(rows.map((r) => r.getAttribute("data-kind")).sort()).toEqual(["backup", "desk"]);
    expect(screen.getByText(t("panel.bg.deskRun", { desk: "Futuros" }))).toBeInTheDocument();
  });

  it("B3: one read every 5 s while the section is open, every 60 s when it is not", async () => {
    vi.useFakeTimers();
    const count = () => fetched.filter((u) => u.startsWith("/api/backup")).length;
    const view = render(<BackgroundFeed open>{null}</BackgroundFeed>);
    await vi.advanceTimersByTimeAsync(10_500);
    expect(count()).toBe(3);
    const before = count();
    view.rerender(<BackgroundFeed open={false}>{null}</BackgroundFeed>);
    await vi.advanceTimersByTimeAsync(10_500);
    expect(count() - before).toBe(1); // the one read the change of period makes; the next one is 60 s away
  });
});

describe("The panel", () => {
  it("S1: four sections in order; folding one is remembered; without storage all start open", async () => {
    renderShell();
    const panel = screen.getByRole("complementary", { name: t("rightPanel.title") });
    const heads = within(panel).getAllByRole("button", { expanded: true }).map((b) => b.textContent);
    expect(heads).toEqual([t("rightPanel.section.project"), t("rightPanel.section.agents"), t("rightPanel.section.background"), t("rightPanel.section.results")].map((s) => expect.stringContaining(s)));
    await userEvent.click(within(panel).getByRole("button", { name: new RegExp(t("rightPanel.section.agents")) }));
    expect(JSON.parse(localStorage.getItem("hokma.rightPanel.sections") ?? "{}")).toEqual({ agents: true });
    expect(within(panel).getByRole("button", { name: new RegExp(t("rightPanel.section.agents")) })).toHaveAttribute("aria-expanded", "false");
  });

  it("T3 T4: minimized it is a rail that counts agents and background, and opening it loses nothing", async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ sidebar: 0, rightPanel: false, terminal: false }));
    payloads["/api/scheduled-jobs"] = { jobs: [{ id: 5, name: "Rutina", agent: "iris", agent_name: "Iris", last: { task_id: 1, status: "running", summary: "" } }], running: [5] };
    renderWithApp(<RightPanel />);
    startTurn();
    agentStart("a1", "iris");
    server.emit({ type: "agent_step", ...base, agent_id: "a1", step: step("s1") });
    const rail = await screen.findByTestId("panel-rail");
    await waitFor(() => expect(rail).toHaveAttribute("aria-label", `${t("rightPanel.rail.agents", { n: 2 })}, ${t("rightPanel.rail.background.one")}. ${t("rightPanel.rail.open")}`));
    expect(screen.getByTestId("rail-agents")).toHaveTextContent("2");
    expect(screen.getByTestId("rail-background")).toHaveTextContent("1");
    await userEvent.click(rail);
    const row = await screen.findByTestId("agent-row");
    expect(row).toHaveTextContent("data/prices.csv");
  });
});

describe("Panel C: what asks for a decision is in the chat, the panel reflects it", () => {
  const block = { type: "guard_block", ...base, id: 9, rule: "delete", tool: "Bash", target: "rm -rf informes", at: "2026-11-05 12:25:00" };
  const runCommand = () => {
    server.emit({ type: "tool_use", ...base, tool_use_id: "c1", name: "Bash", input: {}, summary: { kind: "run_command", target: "git clone x" } });
    server.emit({ type: "tool_output", ...base, tool_use_id: "c1", text: "Cloning into 'x'...\n" });
  };

  it("C1: a block is a line in the panel, with no card and no action; the card is in the chat", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    server.emit(block);
    const row = await screen.findByTestId("turn-row");
    expect(row).toHaveTextContent(t("panel.agents.blocked.one"));
    await userEvent.click(within(row).getByRole("button", { name: t("panel.agents.viewSteps") }));
    expect(screen.getByTestId("block-line")).toHaveTextContent(t("block.title.delete"));
    expect(screen.getByTestId("block-line")).toHaveTextContent("rm -rf informes");
    expect(screen.queryByTestId("block-card")).toBeNull();
    expect(within(row).queryByRole("button", { name: t("block.viewTrash") })).toBeNull();
  });

  it("C2: the live output of a command is inside View steps, folded until it is opened", async () => {
    renderWithApp(<AgentsSection />);
    startTurn();
    runCommand();
    const row = await screen.findByTestId("turn-row");
    await userEvent.click(within(row).getByRole("button", { name: t("panel.agents.viewSteps") }));
    await userEvent.click(within(row).getByText(t("panel.agents.steps", { n: 1 })));
    const output = screen.getByTestId("tool-output");
    expect(output).not.toHaveAttribute("open");
    expect(output).toHaveTextContent("Cloning into 'x'...");
    await userEvent.click(within(output).getByText(t("panel.step.output")));
    expect(output).toHaveAttribute("open");
    server.emit({ type: "tool_result", ...base, tool_use_id: "c1", is_error: false, text: "done" });
    expect(screen.queryByTestId("tool-output")).toBeNull();
  });

  it("C3: a routine whose last run is archived says Archivada", async () => {
    payloads["/api/scheduled-jobs"] = { jobs: [{ id: 5, name: "Resumen", agent: "iris", agent_name: "Iris", last: { task_id: 1, status: "archived", summary: "", started_at: new Date(Date.now() - 90_000).toISOString(), ended_at: new Date(Date.now() - 60_000).toISOString() } }], running: [] };
    renderWithApp(
      <BackgroundFeed open>
        <BackgroundSection />
      </BackgroundFeed>,
    );
    await userEvent.click(await screen.findByText(t("panel.bg.finished", { n: 1 })));
    expect(await screen.findByText(t("panel.bg.state.archived"))).toBeInTheDocument();
    expect(t("panel.bg.state.archived")).toBe("Archivada");
  });

  it("C4: the minimized strip turns amber for a pending permission and for a block, and goes back when the panel is seen", async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ sidebar: 0, rightPanel: false, terminal: false }));
    renderWithApp(<RightPanel />);
    startTurn();
    const strip = await screen.findByLabelText(t("rightPanel.rail.label"));
    expect(strip).toHaveAttribute("data-tone", "working");
    server.emit({ type: "permission_request", ...base, request_id: "r1", tool_use_id: "s1", tool: "Read", input: {}, summary: { kind: "read_file", target: "a" }, expires_at: 1 });
    expect(strip).toHaveAttribute("data-tone", "waiting");
    server.emit({ type: "permission_resolved", conversation_id: "7", request_id: "r1", decision: "allow", by: "human" });
    expect(strip).toHaveAttribute("data-tone", "working");
    server.emit(block);
    expect(strip).toHaveAttribute("data-tone", "blocked");
    expect(screen.getByTestId("panel-rail")).toHaveAttribute("aria-label", expect.stringContaining(t("rightPanel.rail.blocked")));
    await userEvent.click(screen.getByTestId("panel-rail"));
  });

  it("C4: the minimized strip also turns amber while an order waits for an answer, and goes back when it is answered (integration 3387 and 3390b)", async () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ sidebar: 0, rightPanel: false, terminal: false }));
    renderWithApp(<RightPanel />);
    server.hub.watch(() => undefined);                       // opens the socket of the hub: nothing else in this test sends
    server.open();
    const strip = await screen.findByLabelText(t("rightPanel.rail.label"));
    expect(strip).toHaveAttribute("data-tone", "idle");
    act(() => {
      server.emit({ type: "order_notice", proposal_id: 9, desk_id: 1, desk: "Mesa", account: "***1", rules: ["size_premium"], whys: ["w"], timeout_min: 30, expires_at: null, stage: "ask", order: "BUY 1" } as never);
    });
    expect(strip).toHaveAttribute("data-tone", "waiting");
    act(() => {
      server.emit({ type: "order_notice_closed", proposal_id: 9, outcome: "followed" } as never);
    });
    expect(strip).toHaveAttribute("data-tone", "idle");
  });
});

describe("T7: one reducer", () => {
  it("the chat of a desk uses the reducer and the items of the chat, and has no tool list of its own", () => {
    const sources = import.meta.glob("../desks/DeskChat.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    const source = Object.values(sources)[0] ?? "";
    expect(source).toContain('from "../chat/state"');
    expect(source).toContain("WorkingLine");
    expect(source).not.toMatch(/function reducer|useReducer\(\(|ToolLine|kind: "tool"/);
  });
});
