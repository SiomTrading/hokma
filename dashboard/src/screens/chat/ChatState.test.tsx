import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { ChatScreen, resetUsageWarning } from "./ChatScreen";
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
let usage: { pct: number | null } = { pct: null };
let listing: unknown[] = [];
let archivedListing: unknown[] = [];

function mockFetch() {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: Call = { url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      if (url.startsWith("/api/usage/weekly")) return new Response(JSON.stringify(usage));
      if (url.startsWith("/api/chat/options")) return new Response(JSON.stringify(OPTIONS));
      if (url.startsWith("/api/conversations") && call.method === "GET") {
        return new Response(JSON.stringify({ conversations: url.includes("archived=true") ? archivedListing : listing }));
      }
      return new Response("{}", { status: 200 });
    }),
  );
}

let server: FakeServer;
const box = () => screen.getByLabelText(t("chat.composer.label"));
const conv = (over: Record<string, unknown> = {}) => ({
  id: "3", title: "Weekly notes", pinned: false, archived: false, model: "sonnet", agent: null, project_id: null,
  message_count: 2, started_at: "2026-11-01T10:00:00+00:00", last_message_at: "2026-11-01T10:05:00+00:00", ...over,
});

beforeEach(() => {
  setToken(TEST_TOKEN);
  usage = { pct: null };
  listing = [];
  archivedListing = [];
  resetUsageWarning();
  mockFetch();
});
afterEach(() => {
  setHub(null);
  vi.unstubAllGlobals();
});

function open(conversation = "9") {
  server = new FakeServer();
  const view = renderWithApp(<ChatScreen tabKey={`chat:conversation=${conversation}`} params={{ conversation }} active />);
  server.open();
  return view;
}

describe("history restores attachments and tool lines", () => {
  it("paints the chips with the image thumbnail and the tool lines where they were called", async () => {
    open("9");
    server.emit({
      type: "history",
      conversation_id: "9",
      messages: [
        {
          id: "1", role: "user", text: "What do you see?", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1,
          attachments: [
            { id: "12", name: "ES-5m.png", mime: "image/png", size: 2048, kind: "image" },
            { id: "13", name: "plan.pdf", mime: "application/pdf", size: 4096, kind: "file" },
          ],
        },
        {
          id: "2", role: "assistant", text: "I will look. Done.", status: "complete", tokens_in: 1, tokens_out: 1, created_at: 2,
          tools: [{ id: "tu1", name: "Read", kind: "read_file", target: "notes.md", at: 12, is_error: false, result: "file text" }],
        },
      ],
    });
    const user = await screen.findByTestId("user-message");
    const thumb = user.querySelector("img.chat-thumb");
    expect(thumb?.getAttribute("src")).toBe(`/api/attachments/12?token=${TEST_TOKEN}`);
    expect(within(user).getByText("ES-5m.png")).toBeInTheDocument();
    expect(within(user).getByText("plan.pdf")).toBeInTheDocument();
    expect(user.querySelectorAll("img").length).toBe(1);
    // text, tool line, text: the line sits where the tool was called
    const order = Array.from(document.querySelectorAll('[data-testid="assistant-message"], [data-testid="tool-use"]')).map((n) => n.getAttribute("data-testid"));
    expect(order).toEqual(["assistant-message", "tool-use", "assistant-message"]);
    const tool = screen.getByTestId("tool-use");
    expect(within(tool).getByText("notes.md")).toBeInTheDocument();
    expect(within(tool).getByText(t("chat.tool.done"))).toBeInTheDocument();
    expect(within(tool).getByText("file text")).toBeInTheDocument();
  });
});

describe("a turn in progress survives a reconnect", () => {
  it("rebuilds the answer so far from the server and keeps streaming after it", async () => {
    open("9");
    server.emit({ type: "history", conversation_id: "9", messages: [{ id: "1", role: "user", text: "Go", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 }] });
    server.emit({ type: "state", conversation_id: "9", state: "running", pending_permissions: [] });
    server.emit({ type: "delta", conversation_id: "9", turn_id: "t1", text: "Part one " });
    server.drop();
    server.open();
    expect(server.last("open")).toEqual({ type: "open", conversation_id: "9" });
    // the server answers with the saved history plus the text it kept for the live turn
    server.emit({ type: "history", conversation_id: "9", messages: [{ id: "1", role: "user", text: "Go", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 }] });
    server.emit({
      type: "state", conversation_id: "9", state: "running", pending_permissions: [],
      partial: { turn_id: "t1", text: "Part one and two ", tools: [{ id: "tu1", name: "Bash", kind: "run_command", target: "ls", at: 4, is_error: null, result: null }] },
    });
    const bubbles = await screen.findAllByTestId("assistant-message");
    expect(bubbles.map((b) => b.textContent)).toEqual(["Part", "one and two"].map((s) => expect.stringContaining(s)));
    expect(within(screen.getByTestId("tool-use")).getByText(t("chat.tool.running"))).toBeInTheDocument();
    server.emit({ type: "delta", conversation_id: "9", turn_id: "t1", text: "three" });
    await waitFor(() => expect(screen.getAllByTestId("assistant-message").pop()?.textContent).toContain("one and two three"));
    expect(screen.getByRole("button", { name: t("chat.composer.stop") })).toBeInTheDocument();
  });
});

describe("agents:changed", () => {
  it("reloads the agent list for the slash menu", async () => {
    function Emitter() {
      const shell = useShell();
      return <button onClick={() => shell.emit("agents:changed")}>change</button>;
    }
    server = new FakeServer();
    renderWithApp(
      <>
        <ChatScreen tabKey="chat" params={{}} active />
        <Emitter />
      </>,
    );
    server.open();
    await screen.findByText(t("chat.scope.general"));
    const count = () => calls.filter((c) => c.url.startsWith("/api/chat/options")).length;
    const before = count();
    await userEvent.click(screen.getByText("change"));
    await waitFor(() => expect(count()).toBe(before + 1));
  });
});

describe("Opus asks first", () => {
  async function mountNew() {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat" params={{}} active />);
    server.open();
    await screen.findByText(t("chat.scope.general"));
  }
  const pill = () => screen.getByRole("button", { name: /^Claude · / });

  it("keeps Sonnet when the owner cancels and switches only after confirming", async () => {
    await mountNew();
    expect(pill().textContent).toContain("Sonnet");
    await userEvent.click(pill());
    await userEvent.click(screen.getByRole("menuitem", { name: new RegExp(t("chat.model.opus")) }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(t("chat.opus.text"))).toBeInTheDocument();
    expect(t("chat.opus.text")).toMatch(/^Opus .* Sonnet; .* semanal en horas\.$/);
    await userEvent.click(within(dialog).getByRole("button", { name: t("chat.opus.cancel") }));
    expect(pill().textContent).toContain("Sonnet");
    await userEvent.click(pill());
    await userEvent.click(screen.getByRole("menuitem", { name: new RegExp(t("chat.model.opus")) }));
    await userEvent.click(await screen.findByRole("button", { name: t("chat.opus.confirm") }));
    expect(pill().textContent).toContain("Opus");
  });

  it("starts every new conversation on Sonnet", async () => {
    await mountNew();
    await userEvent.type(box(), "hello{Enter}");
    expect(server.last("send")).toMatchObject({ conversation_id: null, text: "hello", model: "sonnet" });
  });
});

describe("weekly cap bar", () => {
  it("turns orange from 80 percent and warns once in the chat", async () => {
    usage = { pct: 85 };
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat" params={{}} active />);
    server.open();
    const bar = await screen.findByTestId("usage-bar");
    expect(bar.querySelector(".chat-usage-fill")).toHaveStyle({ background: "var(--org)", width: "85%" });
    expect(await screen.findByText(t("chat.usage.warn", { pct: 85 }))).toBeInTheDocument();
    expect(screen.getAllByText(t("chat.usage.warn", { pct: 85 })).length).toBe(1);
  });

  it("stays in the accent color under 80 percent and says nothing", async () => {
    usage = { pct: 40 };
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat" params={{}} active />);
    server.open();
    const bar = await screen.findByTestId("usage-bar");
    expect(bar.querySelector(".chat-usage-fill")).toHaveStyle({ background: "var(--acc)" });
    expect(screen.queryByText(/tope semanal/i, { selector: ".pill" })).toBeNull();
  });

  it("shows no bar without a cap", async () => {
    usage = { pct: null };
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat" params={{}} active />);
    server.open();
    await screen.findByText(t("chat.scope.general"));
    await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/usage/weekly"))).toBe(true));
    expect(screen.queryByTestId("usage-bar")).toBeNull();
  });
});

describe("archive", () => {
  it("archives from the row menu and unarchives from the archived list", async () => {
    listing = [conv()];
    archivedListing = [conv({ id: "4", title: "Old plan", archived: true })];
    server = new FakeServer();
    renderWithApp(<ConversationList query="" />);
    await userEvent.click(await screen.findByRole("button", { name: t("chat.list.options", { title: "Weekly notes" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("chat.list.archive") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH" && c.url === "/api/conversations/3")?.body).toEqual({ archived: true }));

    await userEvent.click(screen.getByRole("button", { name: t("chat.list.archivedShow") }));
    expect(await screen.findByText("Old plan")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "GET" && c.url.includes("archived=true"))).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: t("chat.list.options", { title: "Old plan" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("chat.list.unarchive") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH" && c.url === "/api/conversations/4")?.body).toEqual({ archived: false }));
  });
});
