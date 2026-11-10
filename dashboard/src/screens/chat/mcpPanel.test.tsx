import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { ChatScreen } from "./ChatScreen";
import { FakeServer } from "./testkit";
import { setHub } from "./hub";
import { isPanelCommand, parseCommand, suggestionsFor } from "./commands";

interface Call { url: string; method: string; body?: unknown }
let calls: Call[] = [];
let remote: Array<Record<string, unknown>> = [];

function mockFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const call: Call = { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined };
    calls.push(call);
    const path = url.split("?")[0];
    if (path === "/api/extensions/servers") return new Response(JSON.stringify({ servers: remote }));
    if (path === "/api/extensions") {
      return new Response(JSON.stringify({ plugins: [], marketplaces: [], requests: [], problems: [], database: true, servers: [
        { id: "mcp:files", name: "files", kind: "mcp", enabled: true, target: "/usr/bin/files" },
        { id: "mcp:lauz", name: "lauz", kind: "mcp", enabled: true, target: "https://mcp.lauz.io/mcp", remote: true },
      ] }));
    }
    if (path === "/api/extensions/servers/lauz/connect") return new Response(JSON.stringify({ auth_url: null }));
    if (path === "/api/extensions/servers/lauz/logout") return new Response(JSON.stringify({ revoked: true }));
    if (path.startsWith("/api/chat/options")) return new Response(JSON.stringify({ agents: [], projects: [], default_project: { id: "default", name: "Main" } }));
    if (path.startsWith("/api/conversations") && call.method === "GET") return new Response(JSON.stringify({ conversations: [] }));
    return new Response("{}", { status: 200 });
  }));
}

function Probe() {
  const shell = useShell();
  return <span data-testid="settings">{shell.settings.open ? shell.settings.section : "closed"}</span>;
}

let server: FakeServer;
async function mount() {
  server = new FakeServer();
  renderWithApp(<><ChatScreen tabKey="chat" params={{}} active /><Probe /></>);
  server.open();
  await screen.findByText(t("chat.scope.general"));
}
const box = () => screen.getByLabelText(t("chat.composer.label"));

beforeEach(() => {
  remote = [
    { name: "lauz", url: "https://mcp.lauz.io/mcp", state: "needs_auth", message: "", auth: true, signed_in: false },
    { name: "docs", url: "https://docs.example/mcp", state: "connected", message: "", auth: true, signed_in: true },
    { name: "broken", url: "https://x.example/mcp", state: "error", message: "HTTP 502 from the server", auth: true, signed_in: false },
  ];
  mockFetch();
});
afterEach(() => {
  setHub(null);
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("/mcp command parsing", () => {
  it("is a panel command, not one of the engine's, and only when it has no argument", () => {
    expect(isPanelCommand("/mcp")).toBe(true);
    expect(isPanelCommand(" /MCP ")).toBe(true);
    expect(isPanelCommand("/mcp add foo")).toBe(false);
    expect(isPanelCommand("/model opus")).toBe(false);
    expect(parseCommand("/mcp")).toBeNull();
    expect(suggestionsFor("/m", t, null)?.map((s) => s.key)).toEqual(["model", "mcp"]);
  });
});

describe("/mcp panel in the chat", () => {
  it("opens Hokma's panel with each server and its state, and sends nothing to the engine", async () => {
    await mount();
    await userEvent.type(box(), "/mcp{Enter}");
    const dialog = await screen.findByRole("dialog", { name: t("chat.mcp.title") });
    expect(server.sent.some((m) => m.type === "send" || m.type === "command")).toBe(false);
    expect(await within(dialog).findByText("lauz")).toBeInTheDocument();
    expect(within(dialog).getByText(t("chat.mcp.state.needs_auth"))).toBeInTheDocument();
    expect(within(dialog).getByText(t("chat.mcp.state.connected"))).toBeInTheDocument();
    expect(within(dialog).getByText(t("chat.mcp.state.error"))).toBeInTheDocument();
    expect(within(dialog).getByText("HTTP 502 from the server")).toBeInTheDocument();
    expect(within(dialog).getByText("files")).toBeInTheDocument();
    expect(box()).toHaveValue("");
  });

  it("Connect and Reconnect use the extensions API, Revoke only shows for a signed-in server", async () => {
    await mount();
    await userEvent.type(box(), "/mcp{Enter}");
    const dialog = await screen.findByRole("dialog", { name: t("chat.mcp.title") });
    await within(dialog).findByText("lauz");
    expect(within(dialog).getAllByRole("button", { name: new RegExp(t("chat.mcp.revoke")) })).toHaveLength(1);
    await userEvent.click(within(dialog).getByRole("button", { name: `${t("chat.mcp.connect")} lauz` }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/extensions/servers/lauz/connect")).toBe(true));
    expect(within(dialog).getByRole("button", { name: `${t("chat.mcp.reconnect")} broken` })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: `${t("chat.mcp.revoke")} docs` }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/extensions/servers/docs/logout")).toBe(true));
  });

  it("links to Settings > Extensions > MCP and closes", async () => {
    await mount();
    await userEvent.type(box(), "/mcp{Enter}");
    const dialog = await screen.findByRole("dialog", { name: t("chat.mcp.title") });
    await userEvent.click(within(dialog).getByRole("button", { name: t("chat.mcp.settings") }));
    expect(screen.getByTestId("settings")).toHaveTextContent("extensions");
    expect(localStorage.getItem("hokma.extensions.tab")).toBe("mcp");
    expect(screen.queryByRole("dialog", { name: t("chat.mcp.title") })).not.toBeInTheDocument();
  });
});
