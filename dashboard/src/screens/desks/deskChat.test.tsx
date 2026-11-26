import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { setHub } from "../chat/hub";
import { FakeServer } from "../chat/testkit";
import { DeskChat } from "./DeskChat";
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

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

interface Call {
  url: string;
  method: string;
  body?: unknown;
}

interface Backend {
  calls: Call[];
  conversation: () => Response;
}

let backend: Backend;
let server: FakeServer;

function stubBackend(over: Partial<Backend> = {}) {
  backend = { calls: [], conversation: () => json({ id: "c1", agent_slug: "desk-chief", model: "sonnet" }), ...over };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: Call = { url: String(url), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined };
      backend.calls.push(call);
      if (call.url === "/api/desks/7/conversation") return backend.conversation();
      if (call.url === "/api/desks/7/agents/desk-chief") return json({ ok: true });
      if (call.url === "/api/desks") return json({ desks: [desk()] });
      if (call.url.startsWith("/api/engines")) return json({ connections: [], store_available: true, active: null });
      return json({});
    }),
  );
}

const callsTo = (url: string, method?: string) => backend.calls.filter((c) => c.url === url && (!method || c.method === method));

/** Mounts the chat of the desk, waits for its box and opens the socket with an empty history. */
async function mount(summary: DeskSummary = desk(), history: unknown[] = []) {
  renderWithApp(<DeskChat desk={summary} />);
  await screen.findByRole("textbox", { name: t("desks.chat.input") });
  server.open();
  server.emit({ type: "history", conversation_id: "c1", messages: history });
}

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("es");
  resetDesksStore();
  server = new FakeServer();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setHub(null);
});

describe("chat of the desk chief", () => {
  it("opens the conversation of the desk, asks for its history and shows the empty state", async () => {
    stubBackend();
    await mount();
    expect(callsTo("/api/desks/7/conversation", "POST")).toHaveLength(1);
    expect(server.last("open")).toEqual({ type: "open", conversation_id: "c1" });
    expect(await screen.findByText(t("desks.chat.empty.title"))).toBeInTheDocument();
    expect(screen.getByPlaceholderText(t("desks.chat.placeholder"))).toBeInTheDocument();
  });

  it("shows the stored history of the conversation", async () => {
    stubBackend();
    await mount(desk(), [
      { id: "m1", role: "user", text: "Que setups ves hoy", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 },
      { id: "m2", role: "assistant", text: "Retroceso al VWAP", status: "complete", tokens_in: 10, tokens_out: 20, created_at: 2 },
    ]);
    expect(await screen.findByText("Retroceso al VWAP")).toBeInTheDocument();
    expect(screen.getByText("Que setups ves hoy")).toBeInTheDocument();
    expect(screen.queryByText(t("desks.chat.empty.title"))).not.toBeInTheDocument();
  });

  it("sends the message to the conversation of the desk, streams the answer and reads the desk again when the turn ends", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: t("desks.chat.input") }), "Hola jefe{Enter}");
    expect(server.last("send")).toMatchObject({ type: "send", conversation_id: "c1", text: "Hola jefe" });
    expect(screen.getByText("Hola jefe")).toBeInTheDocument();
    server.emit({ type: "turn_start", conversation_id: "c1", turn_id: "t1", message_id: "m9" });
    server.emit({ type: "delta", conversation_id: "c1", turn_id: "t1", text: "Buen dia, " });
    server.emit({ type: "delta", conversation_id: "c1", turn_id: "t1", text: "reviso el mandato." });
    expect(await screen.findByText("Buen dia, reviso el mandato.")).toBeInTheDocument();
    const before = callsTo("/api/desks", "GET").length;
    server.emit({ type: "done", conversation_id: "c1", turn_id: "t1", message_id: "m10", stopped: false, usage: { tokens_in: 1, tokens_out: 2, cache_read: 0, cost_usd: 0 }, engine_session: null });
    await waitFor(() => expect(callsTo("/api/desks", "GET").length).toBe(before + 1)); // the meter and the sidebar get the new tokens
  });

  it("shows the message that waits for the summary with its state in the chat of the desk, and drops the state when its turn starts (bug 1257)", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: t("desks.chat.input") }), "Hola jefe{Enter}");
    server.emit({ type: "queued", conversation_id: "c1", message_id: "m9", text: "Hola jefe" });
    expect(await screen.findByTestId("waiting-lead")).toHaveTextContent(t("chat.waitingLead"));
    expect(screen.getAllByText("Hola jefe")).toHaveLength(1);
    server.emit({ type: "turn_start", conversation_id: "c1", turn_id: "t1", message_id: "m9" });
    expect(screen.queryByTestId("waiting-lead")).toBeNull();
    expect(screen.getAllByText("Hola jefe")).toHaveLength(1);
  });

  it("with `about` every message starts with the line that names the report, and the screen shows only what the owner typed", async () => {
    stubBackend();
    renderWithApp(<DeskChat desk={desk()} about="Sobre el analisis 12 de AAPL:" />);
    await screen.findByRole("textbox", { name: t("desks.chat.input") });
    server.open();
    server.emit({ type: "history", conversation_id: "c1", messages: [] });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: t("desks.chat.input") }), "Por que se opuso Simon?{Enter}");
    expect(server.last("send")).toMatchObject({ conversation_id: "c1", text: "Sobre el analisis 12 de AAPL:\nPor que se opuso Simon?" });
    expect(screen.getByText("Por que se opuso Simon?")).toBeInTheDocument();
  });

  it("a reloaded history shows what the owner typed, without the line that names the report", async () => {
    stubBackend();
    renderWithApp(<DeskChat desk={desk()} about={t("desks.analysis.about", { id: "12", symbol: "AAPL" })} />);
    await screen.findByRole("textbox", { name: t("desks.chat.input") });
    server.open();
    server.emit({
      type: "history", conversation_id: "c1",
      messages: [{ id: "m1", role: "user", text: `${t("desks.analysis.about", { id: "12", symbol: "AAPL" })}\nPor que se opuso Simon?` }, { id: "m2", role: "user", text: "About analysis 12 of AAPL:\nWhy?" }],
    });
    expect(await screen.findByText("Por que se opuso Simon?")).toBeInTheDocument();
    expect(screen.getByText("Why?")).toBeInTheDocument();
    expect(screen.queryByText(/Sobre el an/)).toBeNull();
    expect(screen.queryByText(/About analysis/)).toBeNull();
  });

  it("warns once the socket is closed instead of losing the message", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    server.drop();
    expect(await screen.findByText(t("chat.reconnecting"))).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: t("desks.chat.input") }), "Hola{Enter}");
    expect(server.sent.filter((m) => m.type === "send")).toHaveLength(0);
  });
});

describe("model of the chief in this desk", () => {
  it("shows the model the desk gave its chief and changes it only in the desk", async () => {
    stubBackend({ conversation: () => json({ id: "c1", agent_slug: "desk-chief", model: "haiku" }) });
    await mount();
    const user = userEvent.setup();
    const button = screen.getByRole("button", { name: t("chat.model.pill", { name: t("chat.model.haiku") }) });
    await user.click(button);
    const menu = await screen.findByRole("menu", { name: t("chat.model.menu") });
    // the three tiers of the desk, with no engine groups from Settings
    expect(within(menu).queryByRole("group")).not.toBeInTheDocument();
    await user.click(within(menu).getByRole("menuitem", { name: new RegExp(t("chat.model.sonnet")) }));
    await waitFor(() => expect(callsTo("/api/desks/7/agents/desk-chief", "PUT")).toHaveLength(1));
    expect(callsTo("/api/desks/7/agents/desk-chief", "PUT")[0]!.body).toEqual({ model: "sonnet" });
    expect(await screen.findByRole("button", { name: t("chat.model.pill", { name: t("chat.model.sonnet") }) })).toBeInTheDocument();
    // nothing global changed: no default engine, no global agent route
    expect(backend.calls.filter((c) => c.method !== "GET" && (c.url.startsWith("/api/engines") || c.url.startsWith("/api/agents")))).toHaveLength(0);
  });

  it("asks for confirmation before Opus and changes nothing until the student confirms", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: t("chat.model.pill", { name: t("chat.model.sonnet") }) }));
    const menu = await screen.findByRole("menu", { name: t("chat.model.menu") });
    await user.click(within(menu).getByRole("menuitem", { name: new RegExp(t("chat.model.opus")) }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(t("agents.opus.warning"))).toBeInTheDocument();
    expect(callsTo("/api/desks/7/agents/desk-chief", "PUT")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: t("agents.opus.confirm") }));
    await waitFor(() => expect(callsTo("/api/desks/7/agents/desk-chief", "PUT")).toHaveLength(1));
    expect(callsTo("/api/desks/7/agents/desk-chief", "PUT")[0]!.body).toEqual({ model: "opus" });
  });
});

describe("token budget in the chat", () => {
  it("shows the warning from 80% and keeps the box open", async () => {
    stubBackend();
    await mount(desk({ budget_state: "warn", tokens_week: 250000 }));
    expect(await screen.findByText(t("desks.chat.budget.warn", { pct: 83 }))).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: t("desks.chat.input") })).toBeEnabled();
  });

  it("at 100% the box is off and the screen says why", async () => {
    stubBackend();
    await mount(desk({ budget_state: "exhausted", tokens_week: 300000 }));
    expect(await screen.findByText(t("desks.chat.budget.exhausted"))).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: t("desks.chat.input") })).toBeDisabled();
  });

  it("when the server refuses a turn for the budget, the chat says it in its own words and closes the box", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: t("desks.chat.input") }), "Hola{Enter}");
    server.emit({ type: "error", conversation_id: "c1", code: "desk_budget_exhausted", message: "budget" });
    expect(await screen.findByRole("alert")).toHaveTextContent(t("desks.chat.budget.exhausted"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: t("desks.chat.input") })).toBeDisabled());
  });

  it("a conversation the server refuses to open for the budget shows the reason and no box", async () => {
    stubBackend({ conversation: () => json({ detail: { code: "desk_budget_exhausted", message: "x" } }, 409) });
    renderWithApp(<DeskChat desk={desk()} />);
    expect(await screen.findByText(t("desks.chat.budget.exhausted.title"))).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("failures of the chat", () => {
  it("offers a retry when the conversation cannot be opened", async () => {
    let fail = true;
    stubBackend({ conversation: () => (fail ? json({ detail: { code: "x", message: "boom" } }, 500) : json({ id: "c1", agent_slug: "desk-chief", model: "sonnet" })) });
    renderWithApp(<DeskChat desk={desk()} />);
    const user = userEvent.setup();
    expect(await screen.findByText("boom")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByRole("textbox", { name: t("desks.chat.input") })).toBeInTheDocument();
    expect(callsTo("/api/desks/7/conversation", "POST")).toHaveLength(2);
  });

  it("answers a permission request of the chief through the same socket", async () => {
    stubBackend();
    await mount();
    const user = userEvent.setup();
    server.emit({
      type: "permission_request", conversation_id: "c1", turn_id: "t1", request_id: "r1", tool_use_id: "u1", tool: "Bash", input: {},
      summary: { kind: "run_command", target: "ls" }, expires_at: Date.now() / 1000 + 60,
    });
    const allow = await screen.findByRole("button", { name: t("chat.perm.approve") }, { timeout: 5000 });
    await user.click(allow);
    expect(server.last("permission_response")).toMatchObject({ conversation_id: "c1", request_id: "r1", decision: "allow" });
  }, 15000);
});

describe("texts of the chat", () => {
  it("has every text in Spanish and English and none with the long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("desks.chat.") || k.startsWith("desks.rules."));
    expect(keys.length).toBeGreaterThan(10);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key]).not.toContain(DASH);
      expect(en[key]).not.toContain(DASH);
    }
  });

  it("says in the Rules tab and in the wizard that rules are written by talking, with no fixed options promised", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    expect(es["desks.rules.text"]).toContain("conversando con el jefe de mesa");
    expect(es["desks.rules.text"]).not.toMatch(/opcion|desplegable|formulario/i);
    expect(en["desks.rules.text"]).toContain("written by talking with the desk chief");
  });
});
