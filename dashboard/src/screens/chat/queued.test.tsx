import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { ChatScreen } from "./ChatScreen";
import { FakeServer } from "./testkit";
import { setHub } from "./hub";
import { initialState, reducer } from "./state";
import type { ChatState } from "./state";
import type { ServerEvent } from "./types";

// Bug 1257 (UX): the owner's message that waits for the summary of the agents shows at once with its state, and the
// state ends when its own turn starts, without a second copy of the message.

const DASH = String.fromCharCode(0x2014);
const ev = (event: Record<string, unknown>) => event as unknown as ServerEvent;
const feed = (events: ServerEvent[], start: ChatState): ChatState => events.reduce((s, event) => reducer(s, { type: "event", event }), start);
const asked = () => reducer(initialState("9"), { type: "local_send", text: "hola", attachments: [] });
const queued = ev({ type: "queued", conversation_id: "9", message_id: "5", text: "hola" });
const users = (state: ChatState) => state.items.filter((item) => item.kind === "user");

describe("the state of the chat for a message that waits", () => {
  it("marks the message already on screen as waiting and gives it its saved id, without a copy", () => {
    const state = feed([queued], asked());
    expect(users(state)).toHaveLength(1);
    expect(users(state)[0]).toMatchObject({ text: "hola", messageId: "5", waiting: true });
  });

  it("shows the message when the screen did not have it (another tab, a reload)", () => {
    const state = feed([queued], initialState("9"));
    expect(users(state)).toHaveLength(1);
    expect(users(state)[0]).toMatchObject({ text: "hola", messageId: "5", waiting: true });
  });

  it("keeps waiting through the turn of the summary and ends when his own turn starts, with one message", () => {
    const summary = ev({ type: "turn_start", conversation_id: "9", turn_id: "t2", message_id: null, origin: "agents" });
    const during = feed([queued, summary, ev({ type: "delta", conversation_id: "9", turn_id: "t2", text: "Done." }), ev({ type: "done", conversation_id: "9", turn_id: "t2", message_id: "6", stopped: false })], asked());
    expect(users(during)[0]).toMatchObject({ waiting: true });
    const mine = feed([ev({ type: "turn_start", conversation_id: "9", turn_id: "t3", message_id: "5" })], during);
    expect(users(mine)).toHaveLength(1);
    expect(users(mine)[0]).toMatchObject({ messageId: "5", waiting: false });
    expect(mine.running).toBe(true);
  });

  it("ends the waiting state when the wait for the init runs out or a Stop starts it: the same turn_start", () => {
    const afterStop = feed([queued, ev({ type: "done", conversation_id: "9", turn_id: "t2", message_id: null, stopped: true }), ev({ type: "turn_start", conversation_id: "9", turn_id: "t3", message_id: "5" })], asked());
    expect(users(afterStop)[0]).toMatchObject({ waiting: false });
  });

  it("marks a message that was never sent and restores that mark from the history", () => {
    const dropped = feed([queued, ev({ type: "queued_dropped", conversation_id: "9", message_id: "5" })], asked());
    expect(users(dropped)[0]).toMatchObject({ waiting: false, unsent: true });
    const history = feed([ev({ type: "history", conversation_id: "9", messages: [{ id: "5", role: "user", text: "hola", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1, unsent: true }] })], initialState("9"));
    expect(users(history)[0]).toMatchObject({ unsent: true });
  });
});

let server: FakeServer;
beforeEach(() => {
  setToken(TEST_TOKEN);
  setLocale("es");
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

describe("the chat screen with a message that waits", () => {
  it("shows the message with its state at once and drops the state when his turn starts", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=9" params={{ conversation: "9" }} active />);
    server.open();
    server.emit({ type: "history", conversation_id: "9", messages: [{ id: "1", role: "user", text: "Investigate", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 }] });
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox"), "hola{Enter}");
    server.emit({ type: "queued", conversation_id: "9", message_id: "5", text: "hola" });
    expect(await screen.findByTestId("waiting-lead")).toHaveTextContent("En espera del jefe, sale apenas termine el resumen");
    expect(screen.getAllByText("hola")).toHaveLength(1);
    server.emit({ type: "turn_start", conversation_id: "9", turn_id: "t3", message_id: "5" });
    expect(screen.queryByTestId("waiting-lead")).toBeNull();
    expect(screen.getAllByText("hola")).toHaveLength(1);
  });

  it("says in the chat that the message was not sent", async () => {
    server = new FakeServer();
    renderWithApp(<ChatScreen tabKey="chat:conversation=9" params={{ conversation: "9" }} active />);
    server.open();
    server.emit({ type: "history", conversation_id: "9", messages: [] });
    server.emit({ type: "history", conversation_id: "9", messages: [{ id: "5", role: "user", text: "hola", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1, unsent: true }] });
    expect(await screen.findByTestId("unsent")).toHaveTextContent(t("chat.unsent"));
  });
});

describe("the texts", () => {
  it("are in Spanish and English, with the wording asked for, and none has the long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    expect(es["chat.waitingLead"]).toBe("En espera del jefe, sale apenas termine el resumen");
    expect(en["chat.waitingLead"]).toBe("Waiting for the lead, it goes as soon as the summary ends");
    for (const key of ["chat.waitingLead", "chat.unsent"]) {
      expect(es[key], key).toBeTruthy();
      expect(en[key], key).toBeTruthy();
      expect(es[key]).not.toContain(DASH);
      expect(en[key]).not.toContain(DASH);
    }
  });
});
