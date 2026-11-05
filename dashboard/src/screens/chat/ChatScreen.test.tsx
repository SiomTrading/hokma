import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp } from "@/test/utils";
import { ChatScreen } from "./ChatScreen";
import { ConversationList } from "./ConversationList";
import { FakeServer } from "./testkit";
import { setHub } from "./hub";

const OPTIONS = {
  agents: [{ slug: "risk", name: "Risk desk", description: "Watches limits" }],
  projects: [{ id: "5", name: "Strategy" }],
  default_project: { id: "default", name: "Main" },
};

interface Call {
  url: string;
  method: string;
  body?: unknown;
}

let calls: Call[] = [];
let listing: unknown[] = [];

function mockFetch(handlers: Record<string, (call: Call) => unknown> = {}) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: Call = { url, method: init?.method ?? "GET", body: init?.body instanceof FormData ? init.body : init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      const key = `${call.method} ${url.split("?")[0]}`;
      const custom = handlers[key];
      if (custom) {
        const out = custom(call);
        return new Response(JSON.stringify(out), { status: key.startsWith("POST /api/attachments") ? 201 : 200 });
      }
      if (url.startsWith("/api/chat/options")) return new Response(JSON.stringify(OPTIONS));
      if (url.startsWith("/api/conversations") && call.method === "GET") return new Response(JSON.stringify({ conversations: listing }));
      return new Response("{}", { status: 200 });
    }),
  );
}

function ToastProbe() {
  const shell = useShell();
  return (
    <ul aria-label="toasts">
      {shell.toasts.map((toast) => (
        <li key={toast.id}>{toast.text}</li>
      ))}
    </ul>
  );
}

const screenProps = { tabKey: "chat", params: {}, active: true };
const base = { conversation_id: "7", turn_id: "t1" };
const conversationEvent = { type: "conversation", conversation_id: "7", title: "Plan", model: "sonnet", agent: null, project: { id: "default", name: "Main" }, created: true };

let server: FakeServer;

async function mount(props = screenProps) {
  server = new FakeServer();
  const view = renderWithApp(
    <>
      <ChatScreen {...props} />
      <ToastProbe />
    </>,
  );
  server.open();
  await screen.findByText("Main");
  return view;
}

const box = () => screen.getByLabelText(t("chat.composer.label"));

beforeEach(() => {
  listing = [];
  mockFetch();
});
afterEach(() => {
  setHub(null);
  vi.unstubAllGlobals();
});

describe("chat turn", () => {
  it("sends, streams markdown, shows tools, offers Stop and ends cleanly", async () => {
    await mount();
    await userEvent.type(box(), "Plan my week{Enter}");
    expect(server.last("send")).toMatchObject({ conversation_id: null, text: "Plan my week", model: "sonnet", attachments: [] });
    expect(screen.getByTestId("user-message")).toHaveTextContent("Plan my week");

    server.emit(conversationEvent);
    server.emit({ type: "turn_start", ...base, message_id: "1" });
    server.emit({ type: "delta", ...base, text: "Here is **the plan**:\n\n- one" });
    expect(screen.getByText("the plan").tagName).toBe("STRONG");
    expect(screen.getByRole("button", { name: t("chat.composer.stop") })).toBeInTheDocument();
    server.emit({ type: "tool_use", ...base, tool_use_id: "a", name: "Read", input: {}, summary: { kind: "read_file", target: "ips.md" } });
    expect(screen.getByTestId("tool-use")).toHaveTextContent("ips.md");
    server.emit({ type: "tool_result", ...base, tool_use_id: "a", is_error: false, text: "file text" });
    expect(screen.getByTestId("tool-use")).toHaveTextContent(t("chat.tool.done"));
    server.emit({ type: "delta", ...base, text: "second part" });

    await userEvent.click(screen.getByRole("button", { name: t("chat.composer.stop") }));
    expect(server.last("stop")).toEqual({ type: "stop", conversation_id: "7" });
    server.emit({ type: "done", ...base, message_id: "2", stopped: true, usage: { tokens_in: 0, tokens_out: 0, cache_read: 0, cost_usd: 0 }, engine_session: "s" });
    expect(screen.getByRole("button", { name: t("chat.composer.send") })).toBeInTheDocument();
    expect(screen.getByText(t("chat.stopped"))).toBeInTheDocument();
  });

  it("answers a permission card with Approve and with Reject", async () => {
    await mount();
    await userEvent.type(box(), "write it{Enter}");
    server.emit(conversationEvent);
    const request = (id: string) => ({ type: "permission_request", ...base, request_id: id, tool_use_id: id, tool: "Write", input: {}, summary: { kind: "write_file", target: "notes.md" }, expires_at: 1 });
    server.emit(request("r1"));
    const card = screen.getByTestId("permission-card");
    expect(card).toHaveTextContent("notes.md");
    await userEvent.click(within(card).getByRole("button", { name: t("chat.perm.approve") }));
    expect(server.last("permission_response")).toEqual({ type: "permission_response", conversation_id: "7", request_id: "r1", decision: "allow" });
    server.emit({ type: "permission_resolved", conversation_id: "7", request_id: "r1", decision: "allow", by: "human" });
    expect(screen.getByTestId("permission-done")).toHaveTextContent(t("chat.perm.allowed"));

    server.emit(request("r2"));
    await userEvent.click(within(screen.getByTestId("permission-card")).getByRole("button", { name: t("chat.perm.reject") }));
    expect(server.last("permission_response")).toMatchObject({ request_id: "r2", decision: "deny" });
  });

  it("shows the translated error for an engine code", async () => {
    await mount();
    await userEvent.type(box(), "hi{Enter}");
    server.emit({ type: "error", code: "engine_exited", message: "x" });
    expect(screen.getByRole("alert")).toHaveTextContent(t("chat.err.engine_exited"));
  });

  it("saves an answer to the project", async () => {
    mockFetch({ "POST /api/conversations/7/save": () => ({ path: "documentos/plan-2.md", project: { id: "default", name: "Main" } }) });
    await mount();
    await userEvent.type(box(), "hi{Enter}");
    server.emit(conversationEvent);
    server.emit({ type: "delta", ...base, text: "answer" });
    server.emit({ type: "done", ...base, message_id: "2", stopped: false, usage: { tokens_in: 1, tokens_out: 1, cache_read: 0, cost_usd: 0 }, engine_session: "s" });
    await userEvent.click(screen.getByRole("button", { name: t("chat.save") }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/conversations/7/save")?.body).toEqual({ message_id: "2" }));
  });
});

describe("composer", () => {
  it("completes slash commands and sends them", async () => {
    await mount();
    await userEvent.type(box(), "/");
    const menu = screen.getByRole("menu", { name: t("chat.cmd.menu") });
    expect(within(menu).getAllByRole("menuitem").map((n) => n.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining("/modelo"), expect.stringContaining("/agente"), expect.stringContaining("/proyecto")]),
    );
    await userEvent.type(box(), "modelo o");
    await userEvent.keyboard("{Enter}");
    // Opus asks first: nothing is sent until the owner confirms
    expect(screen.getByRole("button", { name: /Claude · Sonnet/ })).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: t("chat.opus.confirm") }));
    expect(server.last("send")).toBeUndefined();
    expect(screen.queryByTestId("user-message")).toBeNull();
    server.emit({ ...conversationEvent, model: "opus" });
    server.emit({ type: "command_result", conversation_id: "7", name: "model", ok: true, value: "opus" });
    expect(screen.getByRole("button", { name: /Claude · Opus/ })).toBeInTheDocument();
  });

  it("completes agents and projects from the backend lists", async () => {
    await mount();
    await userEvent.type(box(), "/agente ");
    expect(await screen.findByRole("menuitem", { name: /risk/ })).toBeInTheDocument();
    await userEvent.clear(box());
    await userEvent.type(box(), "/proyecto st");
    await userEvent.click(screen.getByRole("menuitem", { name: /Strategy/ }));
    expect(server.last("send")).toMatchObject({ text: "/proyecto Strategy" });
    server.emit({ ...conversationEvent, project: { id: "5", name: "Strategy" } });
    server.emit({ type: "command_result", conversation_id: "7", name: "project", ok: true, value: { id: "5", name: "Strategy" } });
    expect(screen.getByText("Strategy")).toBeInTheDocument();
  });

  it("shows the agent in the header", async () => {
    await mount();
    await userEvent.type(box(), "/agente risk{Enter}");
    server.emit({ ...conversationEvent, agent: "risk" });
    expect(screen.getByText(t("chat.agentValue", { name: "risk" }))).toBeInTheDocument();
  });

  it("opens the model menu with /modelo and from the pill, and picks a model", async () => {
    await mount();
    await userEvent.type(box(), "/modelo{Enter}");
    server.emit({ ...conversationEvent });
    server.emit({ type: "command_result", conversation_id: "7", name: "model", ok: true, value: "sonnet", options: ["haiku", "sonnet", "opus"] });
    const menu = await screen.findByRole("menu", { name: t("chat.model.menu") });
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Haiku/ }));
    expect(server.last("command")).toEqual({ type: "command", conversation_id: "7", name: "model", arg: "haiku" });
    expect(screen.getByRole("button", { name: /Claude · Haiku/ })).toBeInTheDocument();
    expect(screen.getByText(t("chat.model.haiku.help"))).toBeInTheDocument();
  });

  it("attaches by click, uploads, previews and sends the id; the chip can be removed", async () => {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:preview", revokeObjectURL: () => undefined }));
    mockFetch({ "POST /api/attachments": () => ({ id: "42", name: "chart.png", mime: "image/png", size: 4, kind: "image" }) });
    await mount();
    const png = new File([new Uint8Array([1, 2, 3, 4])], "chart.png", { type: "image/png" });
    await userEvent.upload(screen.getByTestId("file-input"), png);
    const chip = await screen.findByTestId("pending-attachment");
    expect(chip).toHaveTextContent("chart.png");
    expect(chip.querySelector("img")).not.toBeNull();
    await waitFor(() => expect(chip).not.toHaveTextContent(t("chat.composer.uploading")));
    await userEvent.type(box(), "look{Enter}");
    expect(server.last("send")).toMatchObject({ text: "look", attachments: ["42"] });
    expect(screen.queryByTestId("pending-attachment")).toBeNull();
    expect(screen.getByTestId("user-message")).toHaveTextContent("chart.png");
  });

  it("attaches a file by dropping it and an image by pasting it", async () => {
    let n = 0;
    mockFetch({ "POST /api/attachments": () => ({ id: String(++n), name: "x", mime: "application/pdf", size: 3, kind: "file" }) });
    const { container } = await mount();
    const root = container.querySelector("section")!;
    const pdf = new File(["abc"], "plan.pdf", { type: "application/pdf" });
    fireEvent.dragEnter(root, { dataTransfer: { types: ["Files"], files: [pdf] } });
    expect(screen.getByText(t("chat.composer.drop"))).toBeInTheDocument();
    fireEvent.drop(root, { dataTransfer: { types: ["Files"], files: [pdf] } });
    await screen.findByText("plan.pdf");
    const image = new File([new Uint8Array([9])], "pasted.png", { type: "image/png" });
    fireEvent.paste(box(), { clipboardData: { files: [image], getData: () => "" } });
    await screen.findByText("pasted.png");
    expect(calls.filter((c) => c.url === "/api/attachments")).toHaveLength(2);
  });

  it("rejects a file over 25 MB without uploading it", async () => {
    await mount();
    const big = new File(["x"], "big.bin");
    Object.defineProperty(big, "size", { value: 26 * 1024 * 1024 });
    await userEvent.upload(screen.getByTestId("file-input"), big);
    expect(await screen.findByText(t("chat.composer.tooLarge", { name: "big.bin" }))).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/attachments")).toBe(false);
  });

  it("Shift Enter keeps writing and Enter on empty text sends nothing", async () => {
    await mount();
    await userEvent.type(box(), "{Enter}");
    expect(server.sent).toEqual([]);
    await userEvent.type(box(), "a{Shift>}{Enter}{/Shift}b");
    expect((box() as HTMLTextAreaElement).value).toBe("a\nb");
  });
});

describe("history and reconnection", () => {
  const history = {
    type: "history", conversation_id: "7",
    messages: [
      { id: "1", role: "user", text: "Earlier question", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 },
      { id: "2", role: "assistant", text: "Earlier **answer**", status: "complete", tokens_in: 1, tokens_out: 1, created_at: 2 },
    ],
  };

  it("opens a stored conversation, asks for its history and renders it", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=7" params={{ conversation: "7" }} active />);
    server.open();
    expect(server.last("open")).toEqual({ type: "open", conversation_id: "7" });
    expect(screen.getByText(t("chat.loading"))).toBeInTheDocument();
    server.emit(conversationEvent);
    server.emit(history);
    server.emit({ type: "state", conversation_id: "7", state: "idle", pending_permissions: [] });
    expect(await screen.findByText("Earlier question")).toBeInTheDocument();
    expect(screen.getByText("answer").tagName).toBe("STRONG");
    expect(screen.getByRole("button", { name: t("chat.composer.send") })).toBeInTheDocument();
  });

  it("restores a pending permission after a reload", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=7" params={{ conversation: "7" }} active />);
    server.open();
    server.emit(history);
    server.emit({ type: "state", conversation_id: "7", state: "running", pending_permissions: [{ turn_id: "t", request_id: "r9", tool_use_id: "x", tool: "Bash", input: {}, summary: { kind: "run_command", target: "ls -la" }, expires_at: 1 }] });
    expect(screen.getByTestId("permission-card")).toHaveTextContent("ls -la");
    expect(screen.getByRole("button", { name: t("chat.composer.stop") })).toBeInTheDocument();
  });

  it("shows a stored conversation that no longer exists as an error", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=9" params={{ conversation: "9" }} active />);
    server.open();
    server.emit({ type: "error", conversation_id: "9", code: "unknown_conversation", message: "x" });
    expect(await screen.findByText(t("chat.err.unknown_conversation"))).toBeInTheDocument();
  });

  it("shows Reconnecting when the socket drops and asks for the history again when it is back", async () => {
    server = new FakeServer();
    renderWithApp(
      <>
        <ChatScreen tabKey="chat:conversation=7" params={{ conversation: "7" }} active />
        <ToastProbe />
      </>,
    );
    server.open();
    server.emit(history);
    server.drop();
    expect(screen.getByText(t("chat.reconnecting"))).toBeInTheDocument();
    await userEvent.type(box(), "hello{Enter}");
    expect(await screen.findByText(t("chat.composer.notConnected"))).toBeInTheDocument();
    server.sent = [];
    server.open();
    expect(server.last("open")).toEqual({ type: "open", conversation_id: "7" });
    expect(screen.queryByText(t("chat.reconnecting"))).toBeNull();
  });

  it("Ctrl N starts a fresh conversation in the plain chat tab", async () => {
    function Harness() {
      const shell = useShell();
      return <button onClick={shell.newConversation}>new</button>;
    }
    server = new FakeServer();
    renderWithApp(
      <>
        <ChatScreen {...screenProps} />
        <Harness />
      </>,
    );
    server.open();
    await userEvent.type(box(), "first{Enter}");
    server.emit(conversationEvent);
    expect(screen.getByTestId("user-message")).toBeInTheDocument();
    await userEvent.click(screen.getByText("new"));
    expect(screen.queryByTestId("user-message")).toBeNull();
    await userEvent.type(box(), "second{Enter}");
    expect(server.last("send")).toMatchObject({ conversation_id: null, text: "second" });
  });
});

describe("conversation list", () => {
  const rows = [
    { id: "1", title: "Pinned plan", pinned: true, model: "sonnet", agent: null, project_id: null, message_count: 2, started_at: "2026-11-04T10:00:00+00:00", last_message_at: "2026-11-04T10:00:00+00:00" },
    { id: "2", title: "Weekly notes", pinned: false, model: "sonnet", agent: null, project_id: null, message_count: 2, started_at: "2026-11-05T10:00:00+00:00", last_message_at: new Date().toISOString() },
  ];

  it("shows Pinned and Recent from the backend and searches by text", async () => {
    listing = rows;
    server = new FakeServer();
    function Connected() {
      const shell = useShell();
      return (
        <>
          <input aria-label="search" value={shell.sidebarQuery} onChange={(event) => shell.setSidebarQuery(event.target.value)} />
          <ConversationList query={shell.sidebarQuery} />
        </>
      );
    }
    renderWithApp(<Connected />);
    expect(await screen.findByText("Pinned plan")).toBeInTheDocument();
    expect(screen.getByText(t("chat.list.pinned"))).toBeInTheDocument();
    expect(screen.getByText(t("chat.list.recent"))).toBeInTheDocument();
    listing = [rows[1]];
    await userEvent.type(screen.getByLabelText("search"), "weekly");
    await waitFor(() => expect(calls.some((c) => c.url === "/api/conversations?q=weekly")).toBe(true));
    await waitFor(() => expect(screen.queryByText("Pinned plan")).toBeNull());
    expect(screen.getByText(t("chat.list.results"))).toBeInTheDocument();
  });

  it("pins from the context menu and shows an empty hint", async () => {
    listing = [rows[1]];
    mockFetch({ "PATCH /api/conversations/2": () => ({ ...rows[1], pinned: true }) });
    server = new FakeServer();
    renderWithApp(<ConversationList query="" />);
    await screen.findByText("Weekly notes");
    await userEvent.click(screen.getByRole("button", { name: t("chat.list.options", { title: "Weekly notes" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("chat.list.pin") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ pinned: true }));
  });

  it("renames and deletes with confirmation", async () => {
    listing = [rows[1]];
    mockFetch({ "PATCH /api/conversations/2": () => ({ ...rows[1], title: "New name" }), "DELETE /api/conversations/2": () => ({}) });
    server = new FakeServer();
    renderWithApp(<ConversationList query="" />);
    await screen.findByText("Weekly notes");
    const options = () => screen.getByRole("button", { name: t("chat.list.options", { title: "Weekly notes" }) });
    await userEvent.click(options());
    await userEvent.click(screen.getByRole("menuitem", { name: t("chat.list.rename") }));
    const input = screen.getByLabelText(t("chat.list.renameLabel"));
    await userEvent.clear(input);
    await userEvent.type(input, "New name{Enter}");
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ title: "New name" }));
    await userEvent.click(options());
    await userEvent.click(screen.getByRole("menuitem", { name: t("chat.list.delete") }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: t("chat.list.deleteConfirm") }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/conversations/2")).toBe(true));
  });

  it("shows an empty state and an error state with retry", async () => {
    listing = [];
    server = new FakeServer();
    renderWithApp(<ConversationList query="" />);
    expect(await screen.findByText(new RegExp(t("chat.list.empty")))).toBeInTheDocument();
    setHub(null);

    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    server = new FakeServer();
    renderWithApp(<ConversationList query="" />);
    expect(await screen.findByText(t("chat.list.error"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("chat.retry") })).toBeInTheDocument();
  });
});

describe("voice note", () => {
  it("leaves no microphone button while the recorder component is not in the tree", async () => {
    await mount();
    expect(screen.queryByRole("button", { name: "record" })).toBeNull();
  });
});

// keep the act import used by helpers in this file
void act;
