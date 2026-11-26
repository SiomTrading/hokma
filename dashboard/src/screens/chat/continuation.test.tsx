import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { EMPTY, liveCounts, reduceActivity } from "./activity";
import type { ConvActivity } from "./activity";
import { ChatScreen } from "./ChatScreen";
import { FakeServer } from "./testkit";
import { getHub, setHub } from "./hub";
import { initialState, reducer } from "./state";
import type { ChatState } from "./state";
import type { ServerEvent } from "./types";

// Bug 1257: when an agent started in the background ends, the server opens a turn of its own and the assistant writes
// a message nobody asked for. The chat shows it as a new message with its origin; the detail stays in the side panel.

const ev = (event: Record<string, unknown>) => event as unknown as ServerEvent;
const feed = (events: ServerEvent[], start: ChatState = initialState("9")): ChatState => events.reduce((s, event) => reducer(s, { type: "event", event }), start);
const act = (events: ServerEvent[], start: ConvActivity = EMPTY) => events.reduce((conv, event) => reduceActivity(conv, event, 1000), start);
const first = [
  ev({ type: "turn_start", conversation_id: "9", turn_id: "t1", message_id: "1" }),
  ev({ type: "agent_start", conversation_id: "9", turn_id: "t1", agent_id: "a1", slug: "iris", description: "Investigate", parent: "", started_at: 5 }),
  ev({ type: "delta", conversation_id: "9", turn_id: "t1", text: "Launched." }),
  ev({ type: "done", conversation_id: "9", turn_id: "t1", message_id: "2", stopped: false }),
];
const summary = [
  ev({ type: "turn_start", conversation_id: "9", turn_id: "t2", message_id: null, origin: "agents" }),
  ev({ type: "agent_end", conversation_id: "9", turn_id: "t1", agent_id: "a1", status: "done", ended_at: 9, result: "done" }),
  ev({ type: "delta", conversation_id: "9", turn_id: "t2", text: "Iris finished." }),
  ev({ type: "done", conversation_id: "9", turn_id: "t2", message_id: "3", stopped: false }),
];

describe("the state of the chat takes the turn the server opens by itself", () => {
  it("adds a new assistant message with its origin, without touching the message of the owner", () => {
    const asked = reducer(initialState("9"), { type: "local_send", text: "Investigate", attachments: [] });
    const state = feed([...first, ...summary], asked);
    expect(state.items.map((i) => i.kind)).toEqual(["user", "assistant", "assistant"]);
    const [user, answer, later] = state.items;
    expect(user).toMatchObject({ kind: "user", messageId: "1" });
    expect(answer).toMatchObject({ kind: "assistant", text: "Launched.", messageId: "2", streaming: false });
    expect("origin" in answer!).toBe(false);
    expect(later).toMatchObject({ kind: "assistant", text: "Iris finished.", origin: "agents", messageId: "3", streaming: false });
    expect(state.running).toBe(false);
    expect(state.origin).toBeUndefined();
  });

  it("is running from the moment the turn opens, and the next message of the owner is not marked", () => {
    const open = feed([...first, summary[0]!]);
    expect(open.running).toBe(true);
    const done = feed(summary.slice(1), open);
    const next = feed([ev({ type: "turn_start", conversation_id: "9", turn_id: "t3", message_id: "4" }), ev({ type: "delta", conversation_id: "9", turn_id: "t3", text: "Yes." })], done);
    expect(next.items[next.items.length - 1]).toMatchObject({ text: "Yes." });
    expect("origin" in next.items[next.items.length - 1]!).toBe(false);
  });

  it("restores the mark from the saved history", () => {
    const state = feed([
      ev({
        type: "history", conversation_id: "9",
        messages: [
          { id: "1", role: "user", text: "Investigate", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 },
          { id: "2", role: "assistant", text: "Launched.", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 2 },
          { id: "3", role: "assistant", text: "Iris finished.", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 3, origin: "agents" },
        ],
      }),
    ]);
    expect(state.items.map((i) => (i.kind === "assistant" ? i.origin ?? null : "user"))).toEqual(["user", null, "agents"]);
  });
});

describe("the activity of the panel follows an agent that ends after its turn", () => {
  it("closes the agent in the turn that launched it and opens the summary as its own row", () => {
    const working = act(first);
    expect(liveCounts(working).agents).toBe(1);                 // the turn is over, the agent still works
    const conv = act(summary, working);
    expect(conv.turns.map((turn) => [turn.id, turn.origin ?? null, turn.status])).toEqual([["t1", null, "done"], ["t2", "agents", "done"]]);
    expect(conv.turns[0]!.agents[0]).toMatchObject({ status: "done", result: "done" });
    expect(liveCounts(conv).agents).toBe(0);
  });

  it("does not invent a turn for the end of an agent the panel never saw (the page was reloaded)", () => {
    const conv = act([ev({ type: "agent_end", conversation_id: "9", turn_id: "t1", agent_id: "a1", status: "done", ended_at: 9, result: "done" })]);
    expect(conv.turns).toEqual([]);
    expect(liveCounts(conv).agents).toBe(0);
  });

  it("takes the continuation turn without the text the owner typed for his next message", () => {
    const history = act([ev({ type: "history", conversation_id: "9", messages: [
      { id: "1", role: "user", text: "Investigate", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1000 },
      { id: "3", role: "assistant", text: "Iris finished.", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 2000, origin: "agents",
        agents: [{ id: "a1", slug: "iris", description: "x", parent: "", status: "done", started_at: 1, ended_at: 2, step: null, steps: [], result: "done" }] },
    ] })]);
    expect(history.turns[0]).toMatchObject({ id: "3", prompt: "", origin: "agents" });
  });
});

let server: FakeServer;
beforeEach(() => {
  setToken(TEST_TOKEN);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.startsWith("/api/usage/weekly")) return new Response(JSON.stringify({ pct: null }));
    if (url.startsWith("/api/chat/options")) return new Response(JSON.stringify({ agents: [], projects: [], default_project: { id: "default", name: "Main" } }));
    return new Response(JSON.stringify({}));
  }));
});
afterEach(() => {
  setHub(null);
  vi.unstubAllGlobals();
});

describe("the chat screen", () => {
  it("shows the summary of the agents as a new message with its mark and lets the owner write while they work", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=9" params={{ conversation: "9" }} active />);
    server.open();
    server.emit({ type: "history", conversation_id: "9", messages: [{ id: "1", role: "user", text: "Investigate", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 }] });
    for (const event of first) server.emit(event as unknown as Record<string, unknown>);
    // the turn ended and the agent works: the chat is free (Send is there) and offers Stop for the agents
    expect(await screen.findByRole("button", { name: t("chat.composer.send") })).toBeInTheDocument();
    expect(screen.getByTestId("stop-agents")).toBeInTheDocument();
    expect(screen.queryByTestId("origin-mark")).toBeNull();
    for (const event of summary) server.emit(event as unknown as Record<string, unknown>);
    const mark = await screen.findByTestId("origin-mark");
    expect(mark).toHaveTextContent(t("chat.origin.agents"));
    expect(mark.closest("[data-testid=assistant-message]")).toHaveTextContent("Iris finished.");
    await waitFor(() => expect(screen.queryByTestId("stop-agents")).toBeNull());
    expect(getHub().activity.get("9").turns.map((turn) => turn.origin ?? null)).toEqual([null, "agents"]);
  });
});

describe("the end of an agent after the conversation was reopened", () => {
  it("closes the agent of the saved message even though the socket names the live turn", () => {
    const stored = { id: "2", role: "assistant", text: "Launched.", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1,
      agents: [{ id: "a1", slug: "iris", description: "Investigate", parent: "", status: "working", started_at: 5, ended_at: null, step: null, steps: [], result: null }] };
    const reopened = act([ev({ type: "history", conversation_id: "9", messages: [stored] })]);
    expect(liveCounts(reopened).agents).toBe(1);
    const closed = act([ev({ type: "agent_end", conversation_id: "9", turn_id: "t1", agent_id: "a1", status: "done", ended_at: 9, result: "done" })], reopened);
    expect(closed.turns).toHaveLength(1);
    expect(closed.turns[0]!.agents[0]).toMatchObject({ status: "done", result: "done" });
    expect(liveCounts(closed).agents).toBe(0);
  });
});
