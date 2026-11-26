import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { t } from "@/i18n";
import { parseCommand, suggestionsFor } from "./commands";
import { Markdown, safeHref } from "./markdown";
import { initialState, joinAroundTools, reducer } from "./state";
import type { Action, ChatState } from "./state";
import type { ServerEvent } from "./types";

const ev = (event: Record<string, unknown>): Action => ({ type: "event", event: event as unknown as ServerEvent });
const run = (state: ChatState, ...actions: Action[]) => actions.reduce(reducer, state);

describe("reducer", () => {
  const base = { conversation_id: "7", turn_id: "t1" };

  it("streams deltas into one bubble; tool calls do not enter the chat, they go to the side panel", () => {
    const state = run(
      initialState(null),
      { type: "local_send", text: "hi", attachments: [] },
      ev({ type: "conversation", conversation_id: "7", title: "hi", model: "opus", agent: null, project: { id: "1", name: "Main" }, created: true }),
      ev({ type: "turn_start", ...base, message_id: "11" }),
      ev({ type: "delta", ...base, text: "Hel" }),
      ev({ type: "delta", ...base, text: "lo" }),
      ev({ type: "tool_use", ...base, tool_use_id: "a", name: "Read", input: {}, summary: { kind: "read_file", target: "x.md" } }),
      ev({ type: "tool_result", ...base, tool_use_id: "a", is_error: false, text: "content" }),
      ev({ type: "delta", ...base, text: "Done" }),
      ev({ type: "done", ...base, message_id: "12", stopped: false, usage: { tokens_in: 1, tokens_out: 1, cache_read: 0, cost_usd: 0 }, engine_session: "s" }),
    );
    expect(state.id).toBe("7");
    expect(state.model).toBe("opus");
    expect(state.project?.name).toBe("Main");
    expect(state.running).toBe(false);
    expect(state.items.map((i) => i.kind)).toEqual(["user", "assistant"]);
    const [user, answer] = state.items;
    expect(user).toMatchObject({ messageId: "11" });
    expect(answer).toMatchObject({ text: "Hello\n\nDone", streaming: false, messageId: "12" });
  });

  it("keeps tool lines, command output and agent events out of the chat, and puts a protection block in it (SPEC-013, panel C)", () => {
    const state = run(
      initialState("7"),
      ev({ type: "tool_use", ...base, tool_use_id: "c", name: "run_command", input: {}, summary: { kind: "run_command", target: "git clone x" } }),
      ev({ type: "tool_output", ...base, tool_use_id: "c", text: "Cloning into 'x'...\n" }),
      ev({ type: "tool_result", ...base, tool_use_id: "c", is_error: false, text: "ok" }),
      ev({ type: "guard_block", ...base, id: 1, rule: "real_money", tool: "Bash", target: "x", at: "2026-01-01 00:00:00" }),
      ev({ type: "agent_start", ...base, agent_id: "g", slug: "iris", description: "d", parent: "", started_at: 1 }),
      ev({ type: "agent_step", ...base, agent_id: "g", step: { id: "s", name: "Read", kind: "read_file", target: "a", is_error: null } }),
      ev({ type: "agent_end", ...base, agent_id: "g", status: "done", ended_at: 2, result: "x" }),
      ev({ type: "shell_start", ...base, shell_id: "h", command: "sleep 1", started_at: 1 }),
    );
    expect(state.items.map((i) => i.kind)).toEqual(["block"]);
    expect(state.items[0]).toMatchObject({ block: { id: 1, rule: "real_money", target: "x" } });
  });

  it("starts a new paragraph where a tool was called when the server left no separator, and leaves one that exists", () => {
    const answer = (...events: Action[]) => run(initialState("7"), ...events).items[0] as { text: string };
    const tool = (id: string) => ev({ type: "tool_use", ...base, tool_use_id: id, name: "Read", input: {}, summary: { kind: "read_file", target: "a" } });
    const delta = (text: string) => ev({ type: "delta", ...base, text });
    expect(answer(delta("Voy a leer."), tool("a"), delta("Listo."), tool("b"), delta("Fin.")).text).toBe("Voy a leer.\n\nListo.\n\nFin.");
    expect(answer(delta("Uno.\n\n"), tool("a"), delta("Dos.")).text).toBe("Uno.\n\nDos.");
    expect(answer(delta("Uno. "), tool("a"), delta("Dos.")).text).toBe("Uno. Dos.");
    expect(answer(delta("Uno"), delta("dos")).text).toBe("Unodos");
    expect(answer(delta("Uno."), tool("a"), tool("b"), delta("Dos.")).text).toBe("Uno.\n\nDos.");
  });

  it("the saved answer and the reconnected one join the text around each tool like the live one", () => {
    const tools = (...at: number[]) => at.map((n, i) => ({ id: `t${i}`, name: "Read", kind: "read_file" as const, target: "a", at: n, is_error: false, result: "" }));
    expect(joinAroundTools("Voy a leer.Listo.Fin.", tools(11, 17))).toBe("Voy a leer.\n\nListo.\n\nFin.");
    expect(joinAroundTools("Uno.\n\nDos.", tools(6))).toBe("Uno.\n\nDos.");
    expect(joinAroundTools("Uno.", tools(0, 4))).toBe("Uno.");
    const rebuilt = run(
      initialState("7"),
      ev({ type: "state", conversation_id: "7", state: "running", pending_permissions: [], partial: { turn_id: "t1", text: "Leo.", tools: tools(4) } }),
      ev({ type: "delta", ...base, text: "Listo." }),
    );
    expect((rebuilt.items[0] as { text: string }).text).toBe("Leo.\n\nListo.");
  });

  it("marks a stopped turn and keeps the partial text", () => {
    const state = run(
      initialState("7"),
      ev({ type: "delta", ...base, text: "part" }),
      ev({ type: "done", ...base, message_id: "9", stopped: true, usage: { tokens_in: 0, tokens_out: 0, cache_read: 0, cost_usd: 0 }, engine_session: null }),
    );
    expect(state.items[0]).toMatchObject({ text: "part", status: "stopped", streaming: false });
  });

  it("tracks a permission request until it is resolved and never duplicates it", () => {
    const request = { type: "permission_request", ...base, request_id: "r1", tool_use_id: "a", tool: "Write", input: {}, summary: { kind: "write_file", target: "a.txt" }, expires_at: 1 };
    let state = run(initialState("7"), ev(request), ev(request));
    expect(state.items.filter((i) => i.kind === "permission")).toHaveLength(1);
    state = run(state, ev({ type: "permission_resolved", conversation_id: "7", request_id: "r1", decision: "deny", by: "timeout" }));
    expect(state.items[0]).toMatchObject({ resolution: { decision: "deny", by: "timeout" } });
  });

  it("history replaces the list and pending permissions come back with the state", () => {
    const state = run(
      initialState("7"),
      ev({ type: "history", conversation_id: "7", messages: [
        { id: "1", role: "user", text: "q", status: "complete", tokens_in: 0, tokens_out: 0, created_at: 1 },
        { id: "2", role: "assistant", text: "a", status: "stopped", tokens_in: 0, tokens_out: 0, created_at: 2 },
      ] }),
      ev({ type: "state", conversation_id: "7", state: "running", pending_permissions: [{ turn_id: "t", request_id: "r9", tool_use_id: "x", tool: "Bash", input: {}, summary: { kind: "run_command", target: "ls" }, expires_at: 1 }] }),
    );
    expect(state.loading).toBe(false);
    expect(state.running).toBe(true);
    expect(state.items.map((i) => i.kind)).toEqual(["user", "assistant", "permission"]);
    expect(state.items[1]).toMatchObject({ status: "stopped" });
  });

  it("an error ends the turn with a translated notice; busy leaves the running turn alone", () => {
    const text = (code: string) => `E:${code}`;
    const failed = run(initialState("7"), { type: "event", errorText: text, event: { type: "error", code: "engine_exited", message: "x", conversation_id: "7" } });
    expect(failed.running).toBe(false);
    expect(failed.items[0]).toMatchObject({ kind: "notice", text: "E:engine_exited\nx" });
    const generic = run(initialState("7"), { type: "event", errorText: text, event: { type: "error", code: "engine_error", message: "UserPromptSubmit hook blocked", conversation_id: "7" } });
    expect(generic.items[0]).toMatchObject({ kind: "notice", text: "E:engine_error\nUserPromptSubmit hook blocked" });
    const plain = run(initialState("7"), { type: "event", errorText: text, event: { type: "error", code: "unknown_model", message: "secret detail", conversation_id: "7" } });
    expect(plain.items[0]).toMatchObject({ kind: "notice", text: "E:unknown_model" });
    const busy = run({ ...initialState("7"), running: true }, { type: "event", errorText: text, event: { type: "error", code: "busy", message: "x" } });
    expect(busy.running).toBe(true);
  });

  it("an unknown conversation while loading becomes a load error", () => {
    const state = run(initialState("7"), { type: "event", errorText: () => "gone", event: { type: "error", code: "unknown_conversation", message: "x" } });
    expect(state).toMatchObject({ loading: false, loadError: "gone" });
  });
});

describe("slash commands", () => {
  it("parses both language names and ignores other slashes", () => {
    expect(parseCommand("/modelo opus")).toEqual({ name: "model", arg: "opus" });
    expect(parseCommand("/agent")).toEqual({ name: "agent", arg: null });
    expect(parseCommand("/project my fund")).toEqual({ name: "project", arg: "my fund" });
    expect(parseCommand("/compact")).toBeNull();
    expect(parseCommand("hello /modelo")).toBeNull();
  });

  const options = {
    agents: [{ slug: "risk", name: "Risk", description: "" }],
    projects: [{ id: "1", name: "Strategy" }],
    default_project: { id: "default", name: "Main" },
  };

  it("completes command names, models, agents and projects", () => {
    expect(suggestionsFor("hello", t, options)).toBeNull();
    expect(suggestionsFor("/", t, options)!.map((s) => s.label)).toEqual(["/modelo", "/agente", "/proyecto", "/mcp"]);
    expect(suggestionsFor("/ag", t, options)!.map((s) => s.label)).toEqual(["/agente"]);
    expect(suggestionsFor("/modelo o", t, options)!.map((s) => s.value)).toEqual(["/modelo opus"]);
    expect(suggestionsFor("/agente ", t, options)!.map((s) => s.key)).toEqual(["none", "risk"]);
    expect(suggestionsFor("/proyecto st", t, options)!.map((s) => s.label)).toEqual(["Strategy"]);
    expect(suggestionsFor("/proyecto ", t, options)!.map((s) => s.label)).toEqual(["Strategy", "Main"]);
  });
});

describe("markdown", () => {
  it("renders structure as elements", () => {
    const { container } = render(<Markdown text={"# Title\n\nSome **bold** and `code`.\n\n- one\n- two\n\n```py\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |"} />);
    expect(container.querySelector("h2")?.textContent).toBe("Title");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("pre code")?.textContent).toBe("print(1)");
    expect(container.querySelectorAll("td")).toHaveLength(2);
  });

  it("shows raw HTML as text and refuses unsafe links", () => {
    const { container } = render(<Markdown text={'<img src=x onerror=alert(1)> <script>alert(1)</script> [bad](javascript:alert(1)) [ok](https://example.com)'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    const links = Array.from(container.querySelectorAll("a"));
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.com"]);
    expect(links[0]!.getAttribute("rel")).toContain("noopener");
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref("mailto:a@b.co")).toBe("mailto:a@b.co");
  });

  it("copes with an unclosed code fence while streaming", () => {
    const { container } = render(<Markdown text={"text\n\n```\npartial"} />);
    expect(container.querySelector("pre code")?.textContent).toBe("partial");
  });
});
