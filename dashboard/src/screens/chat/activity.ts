import type { AgentStatus, GuardBlock, HistoryMessage, PermissionBody, ServerEvent, StoredAgent, StoredShell, SummaryKind, TurnOrigin } from "./types";

/**
 * What the side panel shows of a conversation (SPEC-013): the turns, the agents each one launched, their steps, the
 * commands in the background. It is fed by the same socket events as the chat, but the chat shows none of it: the chat
 * reducer drops every tool and agent event, this store keeps them. One store per hub, one entry per conversation.
 */

export const MAX_LIVE_STEPS = 200;
const MAX_RESULT = 600;
const MAX_OUTPUT = 4000;

export interface Step {
  id: string;
  name: string;
  kind: SummaryKind;
  target: string;
  isError: boolean | null;
  result: string | null;
  /** Order in which the steps of the conversation happened, main thread and agents together. */
  seq: number;
  /** Live output of a command while it runs. */
  output?: string;
}

export interface AgentRow {
  id: string;
  slug: string;
  description: string;
  parent: string;
  status: AgentStatus;
  startedAt: number;
  endedAt: number | null;
  result: string | null;
  /** Id of the step it did last. */
  step: string | null;
  steps: Step[];
}

export interface ShellRow {
  id: string;
  command: string;
  status: "running" | AgentStatus;
  startedAt: number;
  endedAt: number | null;
}

/** One turn of the conversation: the main agent's row, with the agents and commands it started. */
export interface TurnRow {
  id: string;
  prompt: string;
  startedAt: number;
  endedAt: number | null;
  status: AgentStatus;
  /** The text of the error that ended the turn, when one did. */
  error: string;
  steps: Step[];
  blocks: GuardBlock[];
  agents: AgentRow[];
  shells: ShellRow[];
  /** Set when the CLI opened the turn by itself, to tell the model that agents started in the background ended (bug 1257). */
  origin?: TurnOrigin;
}

export interface Pending {
  requestId: string;
  /** Id of the subagent that asks; empty when the main thread does. */
  agentId: string;
}

export interface ConvActivity {
  /** Oldest first. */
  turns: TurnRow[];
  running: boolean;
  pending: Pending[];
  /** The agent of the conversation, as the server names it; null for the general one. */
  agent: string | null;
}

export const EMPTY: ConvActivity = { turns: [], running: false, pending: [], agent: null };

/** Milliseconds from a number (seconds or milliseconds) or a stored date text; 0 when it is neither. */
export function parseTime(value: unknown): number {
  if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
  if (typeof value !== "string" || !value) return 0;
  const text = /([zZ]|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const ms = Date.parse(text);
  return Number.isNaN(ms) ? 0 : ms;
}

function stepOf(raw: { id: string; name: string; kind: SummaryKind; target: string; is_error: boolean | null; result?: string | null }): Step {
  return { id: raw.id, name: raw.name, kind: raw.kind, target: raw.target, isError: raw.is_error, result: raw.result ?? null, seq: 0 };
}

/** The order of the steps of a rebuilt conversation: each turn's main thread, then its agents, turn after turn. */
function numbered(turns: TurnRow[]): TurnRow[] {
  let seq = 0;
  return turns.map((turn) => ({
    ...turn,
    steps: turn.steps.map((s) => ({ ...s, seq: (seq += 1) })),
    agents: turn.agents.map((agent) => ({ ...agent, steps: agent.steps.map((s) => ({ ...s, seq: (seq += 1) })) })),
  }));
}

function nextSeq(conv: ConvActivity): number {
  let top = 0;
  for (const turn of conv.turns) {
    for (const s of turn.steps) top = Math.max(top, s.seq);
    for (const agent of turn.agents) for (const s of agent.steps) top = Math.max(top, s.seq);
  }
  return top + 1;
}

function agentOf(raw: StoredAgent): AgentRow {
  return {
    id: raw.id, slug: raw.slug, description: raw.description, parent: raw.parent, status: raw.status,
    startedAt: raw.started_at, endedAt: raw.ended_at, result: raw.result, step: raw.step, steps: raw.steps.map(stepOf),
  };
}

function shellOf(raw: StoredShell): ShellRow {
  return { id: raw.id, command: raw.command, status: raw.status, startedAt: raw.started_at, endedAt: raw.ended_at };
}

function newTurn(id: string, now: number, prompt = ""): TurnRow {
  return { id, prompt, startedAt: now, endedAt: null, status: "working", error: "", steps: [], blocks: [], agents: [], shells: [] };
}

/** Applies `change` to the turn `id`, creating it (live) when the socket did not announce it, as after a reconnect. */
function onTurn(conv: ConvActivity, id: string | undefined, now: number, change: (turn: TurnRow) => TurnRow, create = true): ConvActivity {
  const index = id ? conv.turns.findIndex((turn) => turn.id === id) : conv.turns.length - 1;
  if (index < 0) {
    // The end of an agent of a turn this panel never saw (the page was reloaded while it worked) is not a new turn.
    if (!id || !create) return conv;
    return { ...conv, turns: [...conv.turns, change(newTurn(id, now))] };
  }
  const turns = conv.turns.slice();
  turns[index] = change(turns[index]!);
  return { ...conv, turns };
}

function onAgent(turn: TurnRow, agentId: string, change: (agent: AgentRow) => AgentRow): TurnRow {
  return { ...turn, agents: turn.agents.map((agent) => (agent.id === agentId ? change(agent) : agent)) };
}

function fromHistory(messages: HistoryMessage[]): TurnRow[] {
  const turns: TurnRow[] = [];
  let prompt = "";
  let askedAt = 0;
  for (const message of messages) {
    if (message.role === "user") {
      prompt = message.text;
      askedAt = parseTime(message.created_at);
    } else if (message.role === "assistant" && ((message.tools?.length ?? 0) > 0 || (message.agents?.length ?? 0) > 0 || (message.blocks?.length ?? 0) > 0)) {
      const status: AgentStatus = message.status === "complete" ? "done" : message.status === "stopped" ? "stopped" : "error";
      const endedAt = parseTime(message.created_at);
      turns.push({
        id: message.id, prompt: message.origin ? "" : prompt, startedAt: askedAt || endedAt, endedAt, status, error: "",
        ...(message.origin ? { origin: message.origin } : {}),
        steps: (message.tools ?? []).map(stepOf), blocks: message.blocks ?? [], agents: (message.agents ?? []).map(agentOf), shells: [],
      });
    }
  }
  return turns;
}

function pendingOf(body: PermissionBody): Pending {
  return { requestId: body.request_id, agentId: body.agent?.id ?? "" };
}

const IGNORED_ERRORS = new Set(["busy", "unknown_request", "unknown_conversation", "bad_json", "bad_message"]);

/** Pure: the activity of one conversation after one socket event. `prompt` is the text of the message that started a turn. */
export function reduceActivity(conv: ConvActivity, event: ServerEvent, now: number, prompt = ""): ConvActivity {
  switch (event.type) {
    case "conversation":
      return { ...conv, agent: event.agent };
    case "turn_start": {
      const base = onTurn(conv, event.turn_id, now, (turn) => ({ ...turn, prompt: turn.prompt || prompt, ...(event.origin ? { origin: event.origin } : {}) }));
      return { ...base, running: true };
    }
    case "tool_use": {
      const seq = nextSeq(conv);
      return onTurn(conv, event.turn_id, now, (turn) =>
        turn.steps.some((s) => s.id === event.tool_use_id) || turn.steps.length >= MAX_LIVE_STEPS
          ? turn
          : { ...turn, steps: [...turn.steps, { id: event.tool_use_id, name: event.name, kind: event.summary.kind, target: event.summary.target, isError: null, result: null, seq }] },
      );
    }
    case "tool_result":
      return onTurn(conv, event.turn_id, now, (turn) => ({
        ...turn,
        steps: turn.steps.map((s) => (s.id === event.tool_use_id ? { ...s, isError: event.is_error, result: event.text.slice(0, MAX_RESULT) } : s)),
      }));
    case "tool_output":
      return onTurn(conv, event.turn_id, now, (turn) => ({
        ...turn,
        steps: turn.steps.map((s) => (s.id === event.tool_use_id && s.result === null ? { ...s, output: ((s.output ?? "") + event.text).slice(-MAX_OUTPUT) } : s)),
      }));
    case "guard_block":
      return onTurn(conv, event.turn_id, now, (turn) =>
        turn.blocks.some((b) => b.id === event.id)
          ? turn
          : { ...turn, blocks: [...turn.blocks, { id: event.id, rule: event.rule, tool: event.tool, target: event.target, at: event.at, ...(event.reason ? { reason: event.reason } : {}) }] },
      );
    case "agent_start":
      return onTurn(conv, event.turn_id, now, (turn) =>
        turn.agents.some((a) => a.id === event.agent_id)
          ? turn
          : {
              ...turn,
              agents: [...turn.agents, { id: event.agent_id, slug: event.slug, description: event.description, parent: event.parent, status: "working", startedAt: event.started_at, endedAt: null, result: null, step: null, steps: [] }],
            },
      );
    case "agent_step": {
      const seq = nextSeq(conv);
      return onTurn(conv, event.turn_id, now, (turn) =>
        onAgent(turn, event.agent_id, (agent) => {
          const step = { ...stepOf(event.step), seq };
          const known = agent.steps.some((s) => s.id === step.id);
          if (!known && agent.steps.length >= MAX_LIVE_STEPS) return { ...agent, step: step.id };
          return {
            ...agent,
            step: step.id,
            steps: known ? agent.steps.map((s) => (s.id === step.id ? { ...s, isError: step.isError } : s)) : [...agent.steps, step],
          };
        }),
        false,
      );
    }
    case "agent_end": {
      // A conversation reopened while the agent works shows its answer under the saved message id, not the live turn id.
      const holder = conv.turns.some((t) => t.id === event.turn_id)
        ? event.turn_id
        : (conv.turns.find((t) => t.agents.some((a) => a.id === event.agent_id))?.id ?? event.turn_id);
      return onTurn(conv, holder, now, (turn) =>
        onAgent(turn, event.agent_id, (agent) => ({ ...agent, status: event.status, endedAt: event.ended_at, result: event.result })),
        false,
      );
    }
    case "shell_start":
      return onTurn(conv, event.turn_id, now, (turn) =>
        turn.shells.some((s) => s.id === event.shell_id)
          ? turn
          : { ...turn, shells: [...turn.shells, { id: event.shell_id, command: event.command, status: "running", startedAt: event.started_at, endedAt: null }] },
      );
    case "shell_end":
      return onTurn(conv, event.turn_id, now, (turn) => ({
        ...turn,
        shells: turn.shells.map((s) => (s.id === event.shell_id ? { ...s, status: event.status, endedAt: event.ended_at } : s)),
      }), false);
    case "permission_request":
      return conv.pending.some((p) => p.requestId === event.request_id) ? conv : { ...conv, pending: [...conv.pending, pendingOf(event)] };
    case "permission_resolved":
      return { ...conv, pending: conv.pending.filter((p) => p.requestId !== event.request_id) };
    case "done":
      return {
        ...onTurn(conv, event.turn_id, now, (turn) => ({ ...turn, status: event.stopped ? "stopped" : "done", endedAt: now })),
        running: false,
        pending: [],
      };
    case "error": {
      if (IGNORED_ERRORS.has(event.code)) return conv;
      const last = conv.turns[conv.turns.length - 1];
      const open = last && last.status === "working" ? last.id : undefined;
      const base = open ? onTurn(conv, open, now, (turn) => ({ ...turn, status: "error", endedAt: now, error: event.message || event.code })) : conv;
      return { ...base, running: false, pending: [] };
    }
    case "history":
      return { ...conv, turns: numbered(fromHistory(event.messages)) };
    case "state": {
      const pending = event.pending_permissions.map(pendingOf);
      if (event.state !== "running") {
        return { ...conv, running: false, pending, turns: conv.turns.map((turn) => (turn.status === "working" ? { ...turn, status: "done", endedAt: now } : turn)) };
      }
      const partial = event.partial;
      if (!partial) return { ...conv, running: true, pending };
      const started = Math.min(now, ...(partial.agents ?? []).map((a) => a.started_at), ...(partial.shells ?? []).map((s) => s.started_at));
      const existing = conv.turns.find((turn) => turn.id === partial.turn_id);
      const live: TurnRow = {
        ...(existing ?? newTurn(partial.turn_id, started)),
        steps: partial.tools.map((t) => ({ ...stepOf(t), ...(t.output ? { output: t.output } : {}) })),
        agents: (partial.agents ?? []).map(agentOf),
        shells: (partial.shells ?? []).map(shellOf),
      };
      const turns = existing ? conv.turns.map((turn) => (turn.id === live.id ? live : turn)) : [...conv.turns, live];
      return { ...conv, running: true, pending, turns: numbered(turns) };
    }
    default:
      return conv;
  }
}

/** Status of a row for the dot: a row that waits for a permission shows it, whatever else it does. */
export function liveStatus(conv: ConvActivity, status: AgentStatus, agentId: string): AgentStatus | "waiting" {
  return status === "working" && conv.pending.some((p) => p.agentId === agentId) ? "waiting" : status;
}

export interface ResultFile {
  path: string;
  /** "Hokma" is the empty string; otherwise the slug of the agent that first wrote it. */
  by: string;
  created: boolean;
}

export function normalizePath(path: string): string {
  const parts: string[] = [];
  for (const piece of path.replace(/\\/g, "/").split("/")) {
    if (piece === "." || (piece === "" && parts.length > 0)) continue;
    parts.push(piece);
  }
  return parts.join("/");
}

/** The files the conversation wrote or edited without error, the most recent first. */
export function resultFiles(conv: ConvActivity): ResultFile[] {
  const touches: { step: Step; by: string }[] = [];
  for (const turn of conv.turns) {
    turn.steps.forEach((step) => touches.push({ step, by: "" }));
    turn.agents.forEach((agent) => agent.steps.forEach((step) => touches.push({ step, by: agent.slug || "?" })));
  }
  touches.sort((x, y) => x.step.seq - y.step.seq);
  const seen = new Map<string, ResultFile>();
  const order: string[] = [];
  for (const { step, by } of touches) {
    if ((step.kind !== "write_file" && step.kind !== "edit_file") || step.isError !== false || !step.target) continue;
    const path = normalizePath(step.target);
    if (!seen.has(path)) seen.set(path, { path, by, created: step.kind === "write_file" });
    const at = order.indexOf(path);
    if (at >= 0) order.splice(at, 1);
    order.push(path);
  }
  return order.reverse().map((path) => seen.get(path)!);
}

/** Agents and commands that still work, main agents included, for the counters and the line of the chat. */
export function liveCounts(conv: ConvActivity): { agents: number; shells: number; waiting: boolean; failedTurn: string | null; blocked: string | null } {
  let agents = 0;
  let shells = 0;
  for (const turn of conv.turns) {
    if (turn.status === "working") agents += 1;
    agents += turn.agents.filter((a) => a.status === "working").length;
    shells += turn.shells.filter((s) => s.status === "running").length;
  }
  const last = conv.turns[conv.turns.length - 1];
  // The id of the last protection block of the conversation: the rail stays amber for it until the panel is opened.
  let blocked: string | null = null;
  for (const turn of conv.turns) for (const block of turn.blocks) blocked = String(block.id);
  return { agents, shells, waiting: conv.pending.length > 0, failedTurn: last && last.status === "error" ? last.id : null, blocked };
}

export class ActivityStore {
  private convs = new Map<string, ConvActivity>();
  private listeners = new Set<() => void>();
  private texts = new Map<string, string>();
  private refs = new Map<string, string>();

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get(id: string | null): ConvActivity {
    return (id && this.convs.get(id)) || EMPTY;
  }

  /** The text a message carries, to name the turn it starts. A new conversation is known by its `client_ref` until the server names it. */
  noteSend(message: Record<string, unknown>): void {
    const key = typeof message.conversation_id === "string" ? message.conversation_id : typeof message.client_ref === "string" ? message.client_ref : "";
    if (key && typeof message.text === "string") this.texts.set(key, message.text);
  }

  apply(event: ServerEvent, now: number = Date.now()): void {
    const id = "conversation_id" in event && typeof event.conversation_id === "string" ? event.conversation_id : null;
    if (!id) return;
    if (event.type === "conversation" && event.client_ref) {
      this.refs.set(event.client_ref, id);
      const text = this.texts.get(event.client_ref);
      if (text !== undefined) this.texts.set(id, text);
    }
    const before = this.convs.get(id) ?? EMPTY;
    // The turn the CLI opens by itself (bug 1257) is not the message the owner wrote: it takes none of the pending texts.
    const owned = event.type === "turn_start" && !event.origin;
    const prompt = owned ? (this.texts.get(id) ?? "") : "";
    const after = reduceActivity(before, event, now, prompt);
    if (owned) this.texts.delete(id);
    if (after === before) return;
    this.convs.set(id, after);
    this.listeners.forEach((listener) => listener());
  }

  /** Tests. */
  reset(): void {
    this.convs.clear();
    this.texts.clear();
    this.refs.clear();
    this.listeners.forEach((listener) => listener());
  }
}
