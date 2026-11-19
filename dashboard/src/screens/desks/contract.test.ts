import { afterEach, describe, expect, it, vi } from "vitest";
import { deskAgentsApi } from "./agentsApi";
import { desksApi } from "./api";
import { deskChatApi } from "./deskChatApi";

const json = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });

function answer(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => json(body)));
}

afterEach(() => vi.unstubAllGlobals());

/** What the real server sends (backend/hokma/desks), read as the screens need it. */
describe("the shapes the desks server really sends", () => {
  it("templates: key, suggested_budget and agent_slug become id, token_budget and slug, and custom is left out", async () => {
    answer({
      templates: [
        { key: "minimal", name: "n", description: "d", suggested_budget: 150000, agents: [{ agent_slug: "desk-chief", role: "chief", model: "sonnet" }] },
        { key: "custom", name: "n", description: "d", suggested_budget: null, agents: [{ agent_slug: "desk-chief", role: "chief", model: "sonnet" }] },
      ],
    });
    expect(await desksApi.templates()).toEqual([{ id: "minimal", token_budget: 150000, agents: [{ slug: "desk-chief", role: "chief", model: "sonnet" }] }]);
  });

  it("agents: agent_slug and status are read, and the server's own role keys are not shown as text", async () => {
    answer([
      { agent_slug: "desk-chief", name: "Desk chief", role: "chief", model: "sonnet", active: true, status: "working", can: "desks.role.chief.can", cannot: "desks.role.chief.cannot", tokens_week: 5, last_action: null, activity: [] },
      { agent_slug: "hugo", name: "Hugo", role: "data", model: "haiku", active: false, status: "off", can: "desks.role.data.can", cannot: "desks.role.data.cannot", tokens_week: 0, last_action: null, activity: [] },
    ]);
    const team = await deskAgentsApi.list(7);
    expect(team.map((a) => [a.slug, a.state])).toEqual([
      ["desk-chief", "working"],
      ["hugo", "off"],
    ]);
    expect(team[0]!.can).toEqual([]);
    expect(team[0]!.cannot).toEqual([]);
  });

  it("desks: the optional account arrives with its balance, and an old answer without it still reads", async () => {
    const base = { id: 7, project_id: 3, name: "Futuros", capital: 10000, currency: "USD", status: "active", template: "minimal" };
    const account = { id: 2, name: "Swing", broker: "Hokma Simulated", balance: { cash: 25000, equity: 25000, currency: "USD" }, as_of: "2026-11-18T12:00:00Z" };
    answer({ desks: [{ ...base, account }, { ...base, id: 8, account: null }, { ...base, id: 9 }] });
    const desks = await desksApi.list();
    expect(desks.map((d) => d.account ?? null)).toEqual([account, null, null]);
    expect(desks[0]!.account!.balance.cash).toBe(25000);
  });

  it("conversation: the numeric id of the database is sent to the chat socket as text", async () => {
    answer({ conversation_id: 12, desk_id: 7, project_id: 3, agent_slug: "desk-chief", model: "sonnet", engine: "claude_code", title: "Futuros", created: true });
    expect(await deskChatApi.conversation(7)).toEqual({ id: "12", agent: "desk-chief", model: "sonnet" });
  });
});
