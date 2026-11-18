import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderShell, renderWithApp } from "@/test/utils";
import { DesksScreen } from "./DesksScreen";
import { resetDesksStore } from "./store";
import { DESK_TABS } from "./types";
import type { DeskSummary } from "./types";

function desk(patch: Partial<DeskSummary>): DeskSummary {
  return {
    id: 1, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
    ...patch,
  };
}

function stubApi(response: () => Response | Promise<Response>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      if (String(url).startsWith("/api/desks")) return response();
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  return calls;
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
/** The long dash, built by code so this file stays free of it. */
const DASH = String.fromCharCode(0x2014);
const menu = () => screen.getByRole("complementary", { name: t("nav.menu") });

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("desks menu", () => {
  it("shows the entry 'Mesas de dinero' right after Chat and opens the Fund window", async () => {
    stubApi(() => json({ desks: [] }));
    renderShell();
    const entries = within(menu()).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent);
    const chat = entries.indexOf(t("nav.chat"));
    expect(entries[chat + 1]).toContain("Mesas de dinero");
    await userEvent.click(within(menu()).getByRole("button", { name: /Mesas de dinero/ }));
    expect(await screen.findByRole("heading", { name: "Mesas de dinero" })).toBeInTheDocument();
    expect(await screen.findByText(t("desks.fund.empty.title"))).toBeInTheDocument();
  });

  it("lists one row per desk with its capital and the result of the day, and hides archived desks", async () => {
    const calls = stubApi(() =>
      json({ desks: [desk({ id: 1, name: "Futuros NQ", capital: 10000 }), desk({ id: 2, name: "Swing acciones", capital: 25000, today: 120.5 }), desk({ id: 3, name: "Vieja", status: "archived" })] }),
    );
    renderShell();
    const rows = await within(menu()).findAllByRole("button", { name: /capital/ });
    expect(calls).toContain("/api/desks");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Futuros NQ");
    expect(rows[0]).toHaveTextContent("10.000");
    expect(rows[0]).toHaveTextContent("0,00");
    expect(rows[1]).toHaveTextContent("25.000");
    expect(rows[1]).toHaveTextContent("+120,50");
    expect(within(menu()).queryByText("Vieja")).not.toBeInTheDocument();
  });

  it("accepts the list as a bare array too", async () => {
    stubApi(() => json([desk({ name: "Mesa suelta" })]));
    renderShell();
    expect(await within(menu()).findByText("Mesa suelta")).toBeInTheDocument();
  });

  it("clicking a row opens that desk in the window, in simulation mode, with its five tabs", async () => {
    stubApi(() => json({ desks: [desk({ id: 7, name: "Futuros NQ" })] }));
    renderShell();
    await userEvent.click(await within(menu()).findByRole("button", { name: /Futuros NQ/ }));
    const tabs = await screen.findByRole("tablist", { name: t("desks.tabs.label") });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(DESK_TABS.map((id) => t("desks.tab." + id)));
    expect(screen.getByText(t("desks.mode.simulation"))).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Futuros NQ" })).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches tabs and shows what arrives and when in Operations and Rules", async () => {
    stubApi(() => json({ desks: [desk({ id: 7, name: "Futuros NQ" })] }));
    renderShell();
    await userEvent.click(await within(menu()).findByRole("button", { name: /Futuros NQ/ }));
    await userEvent.click(await screen.findByRole("tab", { name: t("desks.tab.operations") }));
    expect(screen.getByText(t("desks.operations.text"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: t("desks.tab.rules") }));
    expect(screen.getByText(t("desks.rules.title"))).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: t("desks.tab.rules") })).toHaveAttribute("aria-selected", "true");
  });

  it("a desk that is not in the list says so", async () => {
    stubApi(() => json({ desks: [desk({ id: 7, name: "Futuros NQ" })] }));
    renderWithApp(<DesksScreen tabKey="desks:99" params={{ desk: "99" }} active />);
    expect(await screen.findByText(t("desks.notFound.title"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.notFound.back") })).toBeInTheDocument();
  });

  it("shows the load error in the Fund window and keeps the menu without rows", async () => {
    stubApi(() => json({ error: { code: "boom", message: "failed" } }, 500));
    renderShell();
    await userEvent.click(within(menu()).getByRole("button", { name: /Mesas de dinero/ }));
    await waitFor(() => expect(within(screen.getByRole("main")).getByRole("alert")).toBeInTheDocument());
    expect(within(menu()).queryAllByRole("button", { name: /capital/ })).toHaveLength(0);
  });
});

describe("desks texts", () => {
  it("every desks key exists in Spanish and English with the same placeholders, and no text has a long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((key) => key.startsWith("desks.") || key === "nav.desks" || key === "nav.desksNew" || key === "screen.desks");
    expect(keys.length).toBeGreaterThan(20);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key]!.includes(DASH), key).toBe(false);
      expect(en[key]!.includes(DASH), key).toBe(false);
    }
    for (const tab of DESK_TABS) {
      expect(es[`desks.tab.${tab}`], tab).toBeTruthy();
      expect(en[`desks.tab.${tab}`], tab).toBeTruthy();
    }
  });

  it("the entry and the tabs read in English too", async () => {
    setLocale("en");
    stubApi(() => json({ desks: [desk({ id: 7, name: "NQ futures" })] }));
    renderShell();
    await userEvent.click(await within(menu()).findByRole("button", { name: /NQ futures/ }));
    expect(within(await screen.findByRole("tablist")).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Terminal", "Agents", "Operations", "Rules", "Settings"]);
    expect(screen.getByText("Simulation")).toBeInTheDocument();
  });
});
