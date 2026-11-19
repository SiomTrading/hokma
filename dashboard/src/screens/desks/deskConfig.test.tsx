import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { setToken } from "@/api";
import { TEST_TOKEN } from "@/test/utils";
import { DesksScreen } from "./DesksScreen";
import { resetDesksStore } from "./store";
import type { DeskSummary } from "./types";

function desk(patch: Partial<DeskSummary>): DeskSummary {
  return {
    id: 7, project_id: 10, name: "Futuros NQ", focus: "Intraday futures", capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
    ...patch,
  };
}

interface Call {
  method: string;
  url: string;
  body: unknown;
}

interface Server {
  calls: Call[];
  detail: Record<string, unknown>;
  assetsStatus: number;
  connections: unknown[];
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
/** The long dash, built by code so this file stays free of it. */
const DASH = String.fromCharCode(0x2014);

function stubServer(over: Partial<Server> = {}): Server {
  const server: Server = {
    calls: [],
    detail: { id: 7, name: "Futuros NQ", focus: "Intraday futures", capital: 10000, objective_pct: 3, mandate: "Operar retrocesos al VWAP.", assets: ["NQ=F", { symbol: "ES=F" }] },
    assetsStatus: 200,
    connections: [
      { provider_id: "databento", name: "Databento", enabled: true, asset_classes: ["future"] },
      { provider_id: "alpaca", name: "Alpaca", enabled: false, asset_classes: ["equity"] },
    ],
    ...over,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      server.calls.push({ method, url: String(url), body });
      const path = String(url);
      if (path === "/api/desks") return json({ desks: [desk({}), desk({ id: 8, name: "Swing acciones", capital: 25000, drawdown: 2.1, today: 120.5 })] });
      if (path === "/api/desks/7" && method === "GET") return json(server.detail);
      if (path === "/api/desks/7" && method === "PATCH") return json({ ok: true });
      if (path === "/api/desks/7/assets" && method === "PUT") {
        return server.assetsStatus === 200 ? json({ ok: true }) : json({ detail: { code: "desks.unknown_symbol", message: "That symbol is not in the catalog." } }, server.assetsStatus);
      }
      if (path === "/api/desks/7/connections" && method === "GET") return json({ connections: server.connections });
      if (path.startsWith("/api/desks/7/connections/") && method === "PUT") return json({ ok: true });
      return json({});
    }),
  );
  return server;
}

const sent = (server: Server, method: string, url: string) => server.calls.filter((c) => c.method === method && c.url === url);

function open(tab: string) {
  setToken(TEST_TOKEN);
  return renderWithApp(<DesksScreen tabKey="desks:7" params={{ desk: "7", tab }} active />);
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

describe("desk bar", () => {
  it("shows the desk selector, capital, result of the day, drawdown and a disabled Ctrl K", async () => {
    stubServer();
    open("operations");
    const bar = await screen.findByRole("banner");
    expect(within(bar).getByRole("combobox", { name: t("desks.bar.pick") })).toHaveValue("7");
    expect(within(bar).getAllByRole("option").map((o) => o.textContent)).toEqual(["Futuros NQ", "Swing acciones"]);
    expect(within(bar).getByText("10.000")).toBeInTheDocument();
    expect(within(bar).getByText("0,00")).toBeInTheDocument();
    expect(within(bar).getByText(t("desks.stat.none"))).toBeInTheDocument();
    expect(within(bar).getByRole("button", { name: t("desks.bar.palette") })).toBeDisabled();
  });
});

describe("Operations and Rules", () => {
  it("say what they hold, with no step or version label", async () => {
    stubServer();
    open("operations");
    expect(await screen.findByText(t("desks.operations.title"))).toBeInTheDocument();
    expect(screen.queryByText(/paso \d|pr.xima versi.n/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Reglas" }));
    expect(screen.getByText(t("desks.rules.title"))).toBeInTheDocument();
    expect(screen.queryByText(/paso \d|pr.xima versi.n/i)).not.toBeInTheDocument();
  });
});

describe("Settings: objective and capital", () => {
  it("loads the desk, and editing capital and mandate sends only those two fields and refreshes the list", async () => {
    const server = stubServer();
    open("config");
    const capital = await screen.findByLabelText(t("desks.field.capital"));
    expect(capital).toHaveValue("10000");
    expect(screen.getByLabelText(t("desks.field.name"))).toHaveValue("Futuros NQ");
    expect(screen.getByLabelText(t("desks.cfg.objective"))).toHaveValue("3");
    const save = screen.getByRole("button", { name: t("desks.cfg.save") });
    expect(save).toBeDisabled();
    await userEvent.clear(capital);
    await userEvent.type(capital, "25.000");
    const mandate = screen.getByLabelText(t("desks.cfg.mandate"));
    await userEvent.clear(mandate);
    await userEvent.type(mandate, "Priorizar no perder.");
    await userEvent.click(save);
    expect(await screen.findByText(t("desks.cfg.saved"))).toBeInTheDocument();
    const patches = sent(server, "PATCH", "/api/desks/7");
    expect(patches).toHaveLength(1);
    expect(patches[0]!.body).toEqual({ capital: 25000, mandate: "Priorizar no perder." });
    await waitFor(() => expect(sent(server, "GET", "/api/desks").length).toBeGreaterThan(1));
  });

  it("sets and clears the weekly token cap with the same Save button", async () => {
    const server = stubServer();
    open("config");
    const budget = await screen.findByLabelText(t("desks.cfg.budget"));
    expect(budget).toHaveValue("");
    await userEvent.type(budget, "250.000");
    await userEvent.click(screen.getByRole("button", { name: t("desks.cfg.save") }));
    expect(await screen.findByText(t("desks.cfg.saved"))).toBeInTheDocument();
    expect(sent(server, "PATCH", "/api/desks/7")[0]!.body).toEqual({ token_budget: 250000 });
    await userEvent.clear(budget);
    await userEvent.click(screen.getByRole("button", { name: t("desks.cfg.save") }));
    await waitFor(() => expect(sent(server, "PATCH", "/api/desks/7")).toHaveLength(2));
    expect(sent(server, "PATCH", "/api/desks/7")[1]!.body).toEqual({ token_budget: null });
  });

  it("refuses a capital out of range without calling the server", async () => {
    const server = stubServer();
    open("config");
    const capital = await screen.findByLabelText(t("desks.field.capital"));
    await userEvent.clear(capital);
    await userEvent.type(capital, "50");
    await userEvent.click(screen.getByRole("button", { name: t("desks.cfg.save") }));
    expect(await screen.findByText(/El capital va desde 100/)).toBeInTheDocument();
    expect(sent(server, "PATCH", "/api/desks/7")).toHaveLength(0);
  });

  it("an empty name is refused and a goal above 100 too", async () => {
    const server = stubServer();
    open("config");
    const name = await screen.findByLabelText(t("desks.field.name"));
    await userEvent.clear(name);
    const goal = screen.getByLabelText(t("desks.cfg.objective"));
    await userEvent.clear(goal);
    await userEvent.type(goal, "150");
    await userEvent.click(screen.getByRole("button", { name: t("desks.cfg.save") }));
    expect(await screen.findByText(t("desks.wizard.required"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.wizard.objectiveRange"))).toBeInTheDocument();
    expect(sent(server, "PATCH", "/api/desks/7")).toHaveLength(0);
  });

  it("a failed load shows the error and a retry", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url) === "/api/desks" ? json({ desks: [desk({})] }) : json({ detail: { code: "x", message: "No disponible" } }, 500))));
    open("config");
    expect(await screen.findByText("No disponible")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("ui.error.retry") })).toBeInTheDocument();
  });
});

describe("Settings: menu", () => {
  const entries = ["objective", "connections", "memory", "accounts"] as const;

  it("no longer has an Assets section: the Terminal owns the assets", async () => {
    stubServer();
    open("config");
    await screen.findByLabelText(t("desks.field.capital"));
    const nav = screen.getByRole("navigation", { name: t("desks.cfg.nav") });
    expect(within(nav).getAllByRole("button")).toHaveLength(4);
    expect(within(nav).queryByRole("button", { name: /^Activos$/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(t("desks.wizard.assetsList"))).not.toBeInTheDocument();
  });

  it("shows the text of every entry whichever section is open, and never with the class that hides it", async () => {
    stubServer();
    open("config");
    await screen.findByLabelText(t("desks.field.capital"));
    for (const current of entries) {
      await userEvent.click(within(screen.getByRole("navigation", { name: t("desks.cfg.nav") })).getByRole("button", { name: new RegExp(t(`desks.cfg.section.${current}`)) }));
      const nav = screen.getByRole("navigation", { name: t("desks.cfg.nav") });
      for (const id of entries) {
        const label = within(nav).getByText(t(`desks.cfg.section.${id}`));
        expect(label).toBeVisible();
        expect(label.textContent).toBe(t(`desks.cfg.section.${id}`));
        // `hide-c` is the class of the side menu when it is collapsed: it would blank the text here.
        expect(label.closest(".hide-c")).toBeNull();
      }
      expect(within(nav).getByRole("button", { name: new RegExp(t(`desks.cfg.section.${current}`)) })).toHaveAttribute("aria-current", "page");
    }
  });

  it("marks Trading accounts with a neutral Later pill, no date promised", async () => {
    stubServer();
    open("config");
    await userEvent.click(await screen.findByRole("button", { name: /Cuentas de trading/ }));
    const nav = screen.getByRole("navigation", { name: t("desks.cfg.nav") });
    expect(within(nav).getByText(t("desks.cfg.sim"))).toBeInTheDocument();
    expect(within(nav).queryByText(/^Despu/)).not.toBeInTheDocument();
    expect(getCatalog("en")["desks.cfg.sim"]).toBe("Later");
  });
});

describe("Settings: connections and empty sections", () => {
  it("shows which connections the desk uses and switches one", async () => {
    const server = stubServer();
    open("config");
    await userEvent.click(await screen.findByRole("button", { name: t("desks.cfg.section.connections") }));
    const databento = await screen.findByRole("switch", { name: "Usar Databento" });
    const alpaca = screen.getByRole("switch", { name: "Usar Alpaca" });
    expect(databento).toHaveAttribute("aria-checked", "true");
    expect(alpaca).toHaveAttribute("aria-checked", "false");
    await userEvent.click(alpaca);
    await waitFor(() => expect(alpaca).toHaveAttribute("aria-checked", "true"));
    const puts = sent(server, "PUT", "/api/desks/7/connections/alpaca");
    expect(puts[0]!.body).toEqual({ enabled: true });
    expect(screen.getByText(t("desks.cfg.connections.note"))).toBeInTheDocument();
  });

  it("memory and trading accounts are empty states with no step label", async () => {
    stubServer();
    open("config");
    await userEvent.click(await screen.findByRole("button", { name: /Memoria y mesas conectadas/ }));
    expect(screen.queryByText(/paso \d/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Cuentas de trading/ }));
    expect(screen.getByText(/Todo lo que toque dinero real sigue bloqueado/)).toBeInTheDocument();
  });
});

describe("Settings texts", () => {
  it("the Settings tab reads in English and the new keys exist in both languages without a long dash", async () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((key) => key.startsWith("desks.cfg.") || key.startsWith("desks.bar."));
    expect(keys.length).toBeGreaterThan(30);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key]!.includes(DASH), key).toBe(false);
      expect(en[key]!.includes(DASH), key).toBe(false);
    }
    setLocale("en");
    stubServer();
    open("config");
    expect(await screen.findByLabelText("Capital")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Memory and linked desks" })).toBeInTheDocument();
  });
});
