import { describe, expect, it } from "vitest";
import { ActivityStore, EMPTY, liveCounts, normalizePath, parseTime, reduceActivity, resultFiles } from "./activity";
import type { ConvActivity } from "./activity";
import type { ServerEvent } from "./types";

const base = { conversation_id: "7", turn_id: "t1" };
const ev = (event: Record<string, unknown>) => event as unknown as ServerEvent;
const run = (events: ServerEvent[], start: ConvActivity = EMPTY) => events.reduce((conv, event) => reduceActivity(conv, event, 1000), start);
const step = (id: string, over: Record<string, unknown> = {}) => ({ id, name: "Read", kind: "read_file", target: "a.txt", is_error: null, ...over });

describe("activity of a conversation", () => {
  it("builds the turn, its agents and their steps from the live events", () => {
    const conv = run([
      ev({ type: "turn_start", ...base, message_id: "1" }),
      ev({ type: "agent_start", ...base, agent_id: "a1", slug: "iris", description: "Investigar", parent: "", started_at: 5 }),
      ev({ type: "agent_start", ...base, agent_id: "a2", slug: "hugo", description: "Datos", parent: "a1", started_at: 6 }),
      ev({ type: "agent_step", ...base, agent_id: "a1", step: step("s1") }),
      ev({ type: "agent_step", ...base, agent_id: "a1", step: step("s1", { is_error: false }) }),
      ev({ type: "agent_end", ...base, agent_id: "a1", status: "done", ended_at: 9, result: "hecho" }),
    ]);
    const [turn] = conv.turns;
    expect(turn!.agents.map((a) => [a.id, a.parent, a.status])).toEqual([["a1", "", "done"], ["a2", "a1", "working"]]);
    expect(turn!.agents[0]!.steps).toMatchObject([{ id: "s1", isError: false }]);
    expect(turn!.agents[0]).toMatchObject({ result: "hecho", endedAt: 9, step: "s1" });
    expect(liveCounts(conv)).toMatchObject({ agents: 2, shells: 0 });
  });

  it("tells a stopped turn from a failed one and ignores the errors that are not about the turn", () => {
    const stopped = run([ev({ type: "turn_start", ...base, message_id: "1" }), ev({ type: "done", ...base, message_id: null, stopped: true })]);
    expect(stopped.turns[0]!.status).toBe("stopped");
    const failed = run([ev({ type: "turn_start", ...base, message_id: "1" }), ev({ type: "error", conversation_id: "7", code: "busy", message: "" })]);
    expect(failed.turns[0]!.status).toBe("working");
    const bad = run([ev({ type: "turn_start", ...base, message_id: "1" }), ev({ type: "error", conversation_id: "7", code: "network", message: "sin red" })]);
    expect(bad.turns[0]).toMatchObject({ status: "error", error: "sin red" });
    expect(liveCounts(bad).failedTurn).toBe("t1");
  });

  it("A5: a reconnect brings the live agents and their steps from the snapshot", () => {
    const conv = run([
      ev({ type: "state", conversation_id: "7", state: "running", pending_permissions: [], partial: {
        turn_id: "t1", text: "x", tools: [{ id: "m1", name: "Bash", kind: "run_command", target: "ls", at: 0, is_error: null, result: null }],
        agents: [{ id: "a1", slug: "iris", description: "d", parent: "", status: "working", started_at: 4, ended_at: null, step: "s1", result: null, steps: [{ id: "s1", name: "Read", kind: "read_file", target: "a", is_error: false, result: "r" }] }],
        shells: [{ id: "h", command: "sleep 3", status: "running", started_at: 4, ended_at: null }],
      } }),
    ]);
    expect(conv.running).toBe(true);
    expect(conv.turns[0]).toMatchObject({ id: "t1", status: "working", steps: [{ id: "m1" }], agents: [{ id: "a1", steps: [{ result: "r" }] }], shells: [{ id: "h", status: "running" }] });
  });

  it("A6: a reopened conversation keeps the agents and the tools of each saved answer, with the prompt that asked", () => {
    const conv = run([ev({ type: "history", conversation_id: "7", messages: [
      { id: "1", role: "user", text: "Investiga", status: "complete", tokens_in: 0, tokens_out: 0, created_at: "2026-11-01 10:00:00" },
      { id: "2", role: "assistant", text: "ok", status: "complete", tokens_in: 0, tokens_out: 0, created_at: "2026-11-01 10:01:00",
        tools: [{ id: "m", name: "Write", kind: "write_file", target: "a.md", at: 0, is_error: false, result: null }],
        agents: [{ id: "a1", slug: "iris", description: "d", parent: "", status: "done", started_at: 1, ended_at: 2, step: null, result: "r", steps: [] }] },
      { id: "3", role: "user", text: "Hola", status: "complete", tokens_in: 0, tokens_out: 0, created_at: "2026-11-01 10:02:00" },
      { id: "4", role: "assistant", text: "Hola", status: "complete", tokens_in: 0, tokens_out: 0, created_at: "2026-11-01 10:02:05" },
    ] })]);
    expect(conv.turns).toHaveLength(1);                       // the plain answer is not work
    expect(conv.turns[0]).toMatchObject({ id: "2", prompt: "Investiga", status: "done", steps: [{ id: "m" }], agents: [{ id: "a1" }] });
    expect(conv.turns[0]!.endedAt! - conv.turns[0]!.startedAt).toBe(60_000);
  });

  it("keeps the step of an agent past the live cap out of the list but still ends it without error", () => {
    const events: ServerEvent[] = [ev({ type: "agent_start", ...base, agent_id: "a1", slug: "iris", description: "d", parent: "", started_at: 1 })];
    for (let i = 0; i < 205; i += 1) events.push(ev({ type: "agent_step", ...base, agent_id: "a1", step: step(`s${i}`) }));
    expect(run(events).turns[0]!.agents[0]!.steps).toHaveLength(200);
  });

  it("R1: the files are one row each, the first sight decides created or edited, an error does not count", () => {
    const conv = run([
      ev({ type: "tool_use", ...base, tool_use_id: "w1", name: "Write", input: {}, summary: { kind: "write_file", target: "/p/a.txt" } }),
      ev({ type: "tool_result", ...base, tool_use_id: "w1", is_error: false, text: "" }),
      ev({ type: "agent_start", ...base, agent_id: "a1", slug: "iris", description: "d", parent: "", started_at: 1 }),
      ev({ type: "agent_step", ...base, agent_id: "a1", step: step("e1", { name: "Edit", kind: "edit_file", target: "/p/b.txt", is_error: false }) }),
      ev({ type: "tool_use", ...base, tool_use_id: "e2", name: "Edit", input: {}, summary: { kind: "edit_file", target: "/p/./a.txt" } }),
      ev({ type: "tool_result", ...base, tool_use_id: "e2", is_error: false, text: "" }),
      ev({ type: "tool_use", ...base, tool_use_id: "w2", name: "Write", input: {}, summary: { kind: "write_file", target: "/p/c.txt" } }),
      ev({ type: "tool_result", ...base, tool_use_id: "w2", is_error: true, text: "no" }),
    ]);
    expect(resultFiles(conv)).toEqual([{ path: "/p/a.txt", by: "", created: true }, { path: "/p/b.txt", by: "iris", created: false }]);
    expect(normalizePath("a\\b//c/./d")).toBe("a/b/c/d");
  });

  it("names the turn with the text of the message that started it, also for a new conversation known by its client_ref", () => {
    const store = new ActivityStore();
    store.noteSend({ type: "send", conversation_id: null, client_ref: "draft-1", text: "Investiga el NQ" });
    store.apply(ev({ type: "conversation", conversation_id: "7", title: "", model: "sonnet", agent: null, project: null, created: true, client_ref: "draft-1" }));
    store.apply(ev({ type: "turn_start", ...base, message_id: "1" }));
    expect(store.get("7").turns[0]!.prompt).toBe("Investiga el NQ");
  });

  it("parses seconds, milliseconds and stored dates", () => {
    expect(parseTime(2)).toBe(2000);
    expect(parseTime(1_700_000_000_000)).toBe(1_700_000_000_000);
    expect(parseTime("2026-11-01 10:00:00")).toBe(Date.UTC(2026, 10, 1, 10));
    expect(parseTime(null)).toBe(0);
  });
});
