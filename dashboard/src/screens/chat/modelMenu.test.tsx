import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { Composer } from "./Composer";

// Bug 1095: the model button listed three Claude models whatever was connected. It lists the models of every connected
// engine, by engine, and picking one picks the engine with it.

const CLAUDE = {
  connection_id: null,
  engine: "claude_code",
  label: "Claude",
  models: [
    { id: "haiku", tier: "fast", name: "Haiku" },
    { id: "sonnet", tier: "balanced", name: "Sonnet" },
    { id: "opus", tier: "deep", name: "Opus" },
  ],
};
const GEMINI = {
  connection_id: 2,
  engine: "gemini_cli",
  label: "Gemini",
  models: [
    { id: "haiku", tier: "fast", name: "gemini-2.5-flash-lite" },
    { id: "sonnet", tier: "balanced", name: "gemini-2.5-flash" },
    { id: "opus", tier: "deep", name: "gemini-2.5-pro" },
  ],
};
const CHATGPT = {
  connection_id: 3,
  engine: "codex",
  label: "ChatGPT",
  models: [
    { id: "haiku", tier: "fast", name: "gpt-5-mini" },
    { id: "sonnet", tier: "balanced", name: "gpt-5" },
    { id: "opus", tier: "deep", name: "gpt-5-pro" },
  ],
};

interface Call {
  url: string;
  method: string;
  body?: unknown;
}
let calls: Call[] = [];
let listing: Record<string, unknown>;

function mockEngines(models: unknown[], active: Record<string, unknown>) {
  calls = [];
  listing = { connections: [], detect: { cli: {}, codex_login: false, claude_import: false, providers: [] }, store_available: true, models, active };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: Call = { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      if (url.startsWith("/api/engines/claude")) return new Response(JSON.stringify({ id: 9, engine: "claude_code", label: "Claude" }), { status: 200 });
      if (url.startsWith("/api/engines")) return new Response(JSON.stringify(listing), { status: 200 });
      return new Response("{}", { status: 200 });
    }),
  );
}

function mount(onModel = vi.fn(), conversationId: string | null = "7") {
  renderWithApp(
    <Composer model="sonnet" running={false} stopping={false} options={null} modelMenuRequest={0} conversationId={conversationId} onModel={onModel} onSend={() => true} onStop={() => undefined} />,
  );
  return onModel;
}

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("es");
});
afterEach(() => vi.unstubAllGlobals());

async function openMenu() {
  const user = userEvent.setup();
  await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
  await user.click(screen.getByRole("button", { name: /·/ }));
  return { user, menu: await screen.findByRole("menu", { name: t("chat.model.menu") }) };
}

describe("the model button", () => {
  it("with only Gemini connected it does not show Claude, and the button says Engine · Model", async () => {
    mockEngines([GEMINI], { id: 2, engine: "gemini_cli", label: "Gemini", replaced: false, available: true, saved: null });
    mount();
    const { menu } = await openMenu();
    expect(within(menu).getByRole("group", { name: "Gemini" })).toBeInTheDocument();
    expect(within(menu).getByText("gemini-2.5-flash-lite")).toBeInTheDocument();
    expect(within(menu).queryByRole("group", { name: "Claude" })).not.toBeInTheDocument();
    expect(within(menu).queryByText("Haiku")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Gemini · gemini-2\.5-flash/ })).toBeInTheDocument();
    expect(within(menu).getByText(t("chat.model.connectAnother"))).toBeInTheDocument();
  });

  it("with Claude and ChatGPT connected both groups are there, each with its own models", async () => {
    mockEngines([CLAUDE, CHATGPT], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: true, saved: null });
    mount();
    const { menu } = await openMenu();
    const claude = within(menu).getByRole("group", { name: "Claude" });
    const chatgpt = within(menu).getByRole("group", { name: "ChatGPT" });
    expect(within(claude).getAllByRole("menuitem").map((n) => n.textContent)).toEqual(expect.arrayContaining([expect.stringContaining("Haiku"), expect.stringContaining("Opus")]));
    expect(within(chatgpt).getByText("gpt-5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Claude · Sonnet/ })).toBeInTheDocument();
  });

  it("picking a model of another engine moves the conversation and the default to that engine, with the model", async () => {
    mockEngines([CLAUDE, CHATGPT], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: true, saved: null });
    const onModel = mount();
    const { user, menu } = await openMenu();
    await user.click(within(within(menu).getByRole("group", { name: "ChatGPT" })).getByText("gpt-5-mini"));
    await waitFor(() => expect(onModel).toHaveBeenCalledWith("haiku"));
    expect(calls.find((c) => c.method === "PUT" && c.url === "/api/conversations/7/engine")?.body).toEqual({ connection_id: 3 });
    expect(calls.some((c) => c.method === "PUT" && c.url === "/api/engines/3/default")).toBe(true);
  });

  it("picking Claude when only its virtual entry exists saves its connection first", async () => {
    mockEngines([CLAUDE, GEMINI], { id: 2, engine: "gemini_cli", label: "Gemini", replaced: false, available: true, saved: null });
    const onModel = mount();
    const { user, menu } = await openMenu();
    await user.click(within(within(menu).getByRole("group", { name: "Claude" })).getByText("Opus"));
    await waitFor(() => expect(onModel).toHaveBeenCalledWith("opus"));
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/engines/claude")).toBe(true);
    expect(calls.find((c) => c.method === "PUT" && c.url === "/api/conversations/7/engine")?.body).toEqual({ connection_id: 9 });
  });

  it("picking another model of the same engine still records the choice, without moving the conversation", async () => {
    mockEngines([CLAUDE, CHATGPT], { id: 3, engine: "codex", label: "ChatGPT", replaced: false, available: true, saved: null });
    const onModel = mount();
    const { user, menu } = await openMenu();
    await user.click(within(within(menu).getByRole("group", { name: "ChatGPT" })).getByText("gpt-5-pro"));
    await waitFor(() => expect(onModel).toHaveBeenCalledWith("opus"));
    expect(calls.some((c) => c.method === "PUT" && c.url === "/api/engines/3/default")).toBe(true);   // the last choice
    expect(calls.some((c) => c.method === "PUT" && c.url.endsWith("/engine"))).toBe(false);
  });

  it("a new chat with no conversation yet only sets the default", async () => {
    mockEngines([CLAUDE, CHATGPT], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: true, saved: null });
    const onModel = mount(vi.fn(), null);
    const { user, menu } = await openMenu();
    await user.click(within(within(menu).getByRole("group", { name: "ChatGPT" })).getByText("gpt-5"));
    await waitFor(() => expect(onModel).toHaveBeenCalledWith("sonnet"));
    expect(calls.some((c) => c.method === "PUT" && c.url.endsWith("/engine"))).toBe(false);
    expect(calls.some((c) => c.method === "PUT" && c.url === "/api/engines/3/default")).toBe(true);
  });

  it("with no engine connected it says so and still offers to connect one", async () => {
    mockEngines([], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: false, saved: null });
    mount();
    const user = userEvent.setup();
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalled());
    await user.click(screen.getAllByRole("button").find((b) => b.classList.contains("chat-model"))!);
    const menu = await screen.findByRole("menu", { name: t("chat.model.menu") });
    expect(within(menu).getByText(t("chat.model.none"))).toBeInTheDocument();
    expect(within(menu).getByText(t("chat.model.connectAnother"))).toBeInTheDocument();
  });

  it("the button names the engine that answers, never the first group of the list (bug 1104)", async () => {
    mockEngines([CLAUDE, GEMINI], { id: 2, engine: "gemini_cli", label: "Gemini", replaced: false, available: true, saved: null });
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: /Gemini · / })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Claude · / })).not.toBeInTheDocument();
  });

  it("an engine that answers but is not in the list does not borrow the name of another one", async () => {
    mockEngines([CLAUDE], { id: 2, engine: "gemini_cli", label: "Gemini", replaced: false, available: true, saved: null });
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: /Gemini · / })).toBeInTheDocument());
  });

  it("with no engine available the button says so", async () => {
    mockEngines([], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: false, saved: null });
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: t("chat.model.noEngine") })).toBeInTheDocument());
  });

  it("the list is asked again every time the menu opens, so a connection shows up without restarting", async () => {
    mockEngines([CLAUDE], { id: null, engine: "claude_code", label: "Claude", replaced: false, available: true, saved: null });
    mount();
    const { user } = await openMenu();
    const before = calls.filter((c) => c.url.startsWith("/api/engines")).length;
    await user.keyboard("{Escape}");
    listing.models = [CLAUDE, CHATGPT];
    await user.click(screen.getByRole("button", { name: /·/ }));
    const menu = await screen.findByRole("menu", { name: t("chat.model.menu") });
    await waitFor(() => expect(within(menu).getByRole("group", { name: "ChatGPT" })).toBeInTheDocument());
    expect(calls.filter((c) => c.url.startsWith("/api/engines")).length).toBeGreaterThan(before);
  });
});
