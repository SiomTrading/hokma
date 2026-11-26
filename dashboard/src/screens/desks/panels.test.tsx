import { cleanup, createEvent, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { DeskScreen } from "./DeskScreen";
import { DEFAULT_PANELS, dropAt, missingPanels, moveBy, panelsOf } from "./panels";
import { resetDesksStore } from "./store";
import type { DeskSummary } from "./types";


function desk(patch: Partial<DeskSummary> = {}): DeskSummary {
  return {
    id: 1, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok", ...patch,
  };
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const labels = () => within(screen.getByRole("tablist", { name: t("desks.tabs.label") })).getAllByRole("tab").map((tab) => tab.textContent);
const names = (ids: readonly string[]) => ids.map((id) => t("desks.tab." + id));

/** Answers the calls on the panels like the engine does and records them. */
function stubApi(opts: { failPut?: boolean } = {}) {
  const calls: Array<{ method: string; url: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, url: String(url), body });
      if (String(url).includes("/panels")) {
        if (opts.failPut && method === "PUT") return json({ detail: { code: "bad_panels", message: "No se pudo." } }, 400);
        const order: string[] = method === "PUT" ? body.panels : method === "POST" ? ["terminal", "company", "agents", "operations", "rules", "config"] : ["terminal", "agents", "rules", "config"];
        return json({ panels: order });
      }
      return json({ desks: [] });
    }),
  );
  return calls;
}

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Harness({ initial }: { initial: DeskSummary }) {
  const [current, setCurrent] = useState(initial);
  return (
    <>
      <button type="button" onClick={() => setCurrent({ ...current, panels: ["terminal", "company", "agents", "operations", "rules", "config"] })}>
        servidor cambia
      </button>
      <DeskScreen desk={current} desks={[current]} tabKey="k" />
    </>
  );
}

describe("order of the panels", () => {
  it("a desk without layout has the five of before, in their order", () => {
    expect(panelsOf({})).toEqual([...DEFAULT_PANELS]);
    expect(panelsOf({ panels: null })).toEqual([...DEFAULT_PANELS]);
  });

  it("drops what the screen does not know and puts a missing fixed panel back", () => {
    expect(panelsOf({ panels: ["company", "nope", "company", "terminal"] })).toEqual(["company", "terminal", "rules", "agents", "config"]);
    expect(missingPanels(["terminal", "rules", "agents", "config"])).toEqual(["operations", "company", "calendar", "analysis", "strategies", "option_detail"]);
  });

  it("moves one place and stops at the ends; a drop lands before or after its target", () => {
    expect(moveBy([...DEFAULT_PANELS], "config", -1)).toEqual(["terminal", "agents", "operations", "config", "rules"]);
    expect(moveBy([...DEFAULT_PANELS], "terminal", -1)).toEqual([...DEFAULT_PANELS]);
    expect(moveBy([...DEFAULT_PANELS], "config", 1)).toEqual([...DEFAULT_PANELS]);
    expect(dropAt([...DEFAULT_PANELS], "rules", "terminal", true)).toEqual(["rules", "terminal", "agents", "operations", "config"]);
    expect(dropAt([...DEFAULT_PANELS], "terminal", "config", false)).toEqual(["agents", "operations", "rules", "config", "terminal"]);
  });
});

describe("the tabs of a desk", () => {
  it("P4: shows the five tabs of before when the desk has no layout, and only the removable ones have an x", () => {
    stubApi();
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    expect(labels()).toEqual(names(DEFAULT_PANELS));
    expect(screen.getAllByRole("button", { name: /^Quitar / }).map((b) => b.getAttribute("aria-label"))).toEqual([
      t("desks.panels.remove", { name: t("desks.tab.terminal") }),
      t("desks.panels.remove", { name: t("desks.tab.operations") }),
    ]);
  });

  it("P1: adds Company from the + Agregar menu, with its description, and opens it", async () => {
    const calls = stubApi();
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    await userEvent.click(screen.getByRole("button", { name: t("desks.panels.add") }));
    expect(screen.getByText(t("desks.panel.desc.company"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: new RegExp(t("desks.tab.company")) }));
    await waitFor(() => expect(labels()).toEqual(names(["terminal", "company", "agents", "operations", "rules", "config"])));
    expect(calls.find((c) => c.method === "POST" && c.url.includes("/panels"))).toMatchObject({ url: "/api/desks/1/panels", body: { id: "company" } });
    expect(await screen.findByText(t("desks.company.empty.title"))).toBeInTheDocument();       // the Company panel is the open one
    await userEvent.click(screen.getByRole("button", { name: t("desks.panels.add") }));
    expect(screen.getByText(t("desks.panel.desc.strategies"))).toBeInTheDocument();                       // what is left to add is Strategies
    expect(screen.queryByText(t("desks.panel.desc.company"))).not.toBeInTheDocument();
    expect(screen.getByText(t("desks.panel.desc.calendar"))).toBeInTheDocument();                         // Calendar and Analysis are left to add too
    expect(within(screen.getByRole("list", { name: t("desks.panels.addLabel") })).getAllByRole("listitem")).toHaveLength(4);       // Calendar, Analysis, Strategies and Option detail are left to add
  });

  it("P1: takes Operations off with its x and the screen keeps the rest", async () => {
    const calls = stubApi();
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    await userEvent.click(screen.getByRole("button", { name: t("desks.panels.remove", { name: t("desks.tab.operations") }) }));
    expect(labels()).toEqual(names(["terminal", "agents", "rules", "config"]));
    await waitFor(() => expect(calls.find((c) => c.method === "DELETE")).toMatchObject({ url: "/api/desks/1/panels/operations" }));
  });

  it("P3: a fixed tab moves with the keyboard, the order is saved and the position is announced once", async () => {
    const calls = stubApi();
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    const config = screen.getByRole("tab", { name: t("desks.tab.config") });
    expect(config).toHaveAttribute("aria-keyshortcuts", expect.stringContaining("Control+Shift+ArrowLeft"));
    config.focus();
    await userEvent.keyboard("{Control>}{Shift>}{ArrowLeft}{/Shift}{/Control}");
    expect(labels()).toEqual(names(["terminal", "agents", "operations", "config", "rules"]));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ panels: ["terminal", "agents", "operations", "config", "rules"] }));
    expect(screen.getAllByRole("status").filter((el) => el.textContent === t("desks.panels.moved", { name: t("desks.tab.config"), pos: "4", total: "5" }))).toHaveLength(1);
    expect(screen.getByRole("tab", { name: t("desks.tab.config") })).toHaveFocus();                    // the focus stays on the tab that moved
  });

  it("P3: dragging Rules to the first place moves it and saves the order", async () => {
    const calls = stubApi();
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    const rules = screen.getByRole("tab", { name: t("desks.tab.rules") });
    const target = screen.getByRole("tab", { name: t("desks.tab.terminal") }).parentElement!;
    target.getBoundingClientRect = () => ({ left: 0, width: 100, right: 100, top: 0, bottom: 20, height: 20, x: 0, y: 0, toJSON: () => ({}) });
    const at = (kind: "dragOver" | "drop", clientX: number) => {                                      // jsdom has no DragEvent: the position is set by hand
      const event = createEvent[kind](target);
      Object.defineProperty(event, "clientX", { value: clientX });
      fireEvent(target, event);
    };
    fireEvent.dragStart(rules);
    at("dragOver", 10);
    at("drop", 10);
    expect(labels()).toEqual(names(["rules", "terminal", "agents", "operations", "config"]));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ panels: ["rules", "terminal", "agents", "operations", "config"] }));
  });

  it("a move that the server refuses goes back to the order it had and says so", async () => {
    stubApi({ failPut: true });
    renderWithApp(<DeskScreen desk={desk()} desks={[desk()]} tabKey="k" />);
    screen.getByRole("tab", { name: t("desks.tab.config") }).focus();
    await userEvent.keyboard("{Control>}{Shift>}{ArrowLeft}{/Shift}{/Control}");
    await waitFor(() => expect(labels()).toEqual(names(DEFAULT_PANELS)));
  });

  it("P2: a panel the chief adds by chat shows without reloading when the desk arrives with it", async () => {
    stubApi();
    renderWithApp(<Harness initial={desk()} />);
    expect(labels()).toEqual(names(DEFAULT_PANELS));
    await userEvent.click(screen.getByRole("button", { name: "servidor cambia" }));
    expect(labels()).toEqual(names(["terminal", "company", "agents", "operations", "rules", "config"]));
  });

  it("the desk shows the order the server saved, not the default", () => {
    stubApi();
    const saved = desk({ panels: ["rules", "company", "terminal", "agents", "config"] });
    renderWithApp(<DeskScreen desk={saved} desks={[saved]} tabKey="k" />);
    expect(labels()).toEqual(names(["rules", "company", "terminal", "agents", "config"]));
    expect(screen.getByRole("tab", { name: t("desks.tab.company") }).getAttribute("aria-selected")).toBe("false");
    expect(screen.getByRole("tab", { name: t("desks.tab.terminal") })).toHaveAttribute("aria-selected", "true");
  });
});

describe("texts of the panels", () => {
  it("every key exists in Spanish and English with the same placeholders", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((key) => key.startsWith("desks.panel") || key.startsWith("desks.company.") || key.startsWith("desks.strategies.") || key === "desks.tab.company" || key === "desks.tab.strategies");
    expect(keys.length).toBeGreaterThan(40);
    const holes = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join();
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(holes(en[key]!), key).toBe(holes(es[key]!));
      expect(es[key]).not.toContain(String.fromCharCode(0x2014));
    }
  });
});
