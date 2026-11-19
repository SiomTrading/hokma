import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderShell } from "@/test/utils";
import { resetDesksStore } from "./store";
import { TODAY_EMPTY } from "./todayFixture";
import type { TodayData } from "./todayApi";
import type { DeskSummary } from "./types";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const desk = (patch: Partial<DeskSummary> = {}): DeskSummary => ({
  id: 1, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok", ...patch,
});
const today = (patch: Partial<TodayData> = {}): TodayData => ({ ...TODAY_EMPTY, ...patch });
const now = new Date().toISOString();

interface Call { url: string; method: string; body: unknown }
interface Setup { desks?: DeskSummary[]; today?: () => Response | Promise<Response>; job?: { id: number; approved: boolean } }

function stub(setup: Setup = {}) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ url: String(url), method, body });
    if (url === "/api/desks") return json({ desks: setup.desks ?? [] });
    if (url === "/api/today") return setup.today ? setup.today() : json(TODAY_EMPTY);
    if (url === "/api/scheduled-jobs/template/premarket-briefing") return json({ ...setup.job });
    if (/^\/api\/scheduled-jobs\/\d+\/run$/.test(url)) return json({}, 202);
    return json({});
  }));
  return calls;
}

const openFund = async () => {
  await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
  await screen.findByRole("heading", { name: "Fondo" });
};

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Today card", () => {
  it("shows the watchlist with the desks of each asset, the calendar with its impact and the news", async () => {
    stub({
      desks: [desk()],
      today: () => json(today({
        watchlist: { assets: [{ symbol: "NVDA", last: 186.4, change_pct: 1.8, desks: ["Mesa A"] }, { symbol: "SPY", last: null, error: "x", desks: [] }], as_of: now },
        calendar: { events: [{ time: "2026-11-19T13:30:00Z", country: "USD", event: "Jobless claims", impact: "high", actual: null, estimate: "224K", previous: "228K" }], source: "Forex Factory" },
        news: { news: [{ time: "2026-11-19T13:05:00Z", headline: "Nasdaq sube antes de resultados", source: "Reuters" }] },
      })),
    });
    renderShell();
    await openFund();
    expect(await screen.findByText("NVDA")).toBeInTheDocument();
    expect(screen.getByText("+1,8%")).toBeInTheDocument();
    expect(screen.getByText(t("desks.today.watch.noPrice"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.today.watch.extra"))).toBeInTheDocument();
    expect(screen.getByText("Jobless claims")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: t("desks.today.cal.impact.high") })).toBeInTheDocument();
    expect(screen.getByText("Forex Factory")).toBeInTheDocument();
    expect(screen.getByText("Nasdaq sube antes de resultados")).toBeInTheDocument();
    expect(screen.queryByText(/paso \d/i)).not.toBeInTheDocument();
  });

  it("shows a loading state first, then an error with retry that reloads", async () => {
    let fail = true;
    const calls = stub({ today: () => (fail ? json({ detail: { code: "x", message: "No se pudo leer." } }, 500) : json(TODAY_EMPTY)) });
    renderShell();
    await openFund();
    expect(await screen.findByText("No se pudo leer.")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: t("ui.error.retry") }));
    await waitFor(() => expect(screen.queryByText("No se pudo leer.")).not.toBeInTheDocument());
    expect(calls.filter((c) => c.url === "/api/today").length).toBeGreaterThanOrEqual(2);
  });

  it("tells a missing connection in the blocks and offers Connections", async () => {
    stub({ today: () => json(today({ watchlist: { error: "Conecta Finnhub para ver precios." }, news: { error: "Conecta Finnhub para ver noticias." } })) });
    renderShell();
    await openFund();
    expect(await screen.findByText("Conecta Finnhub para ver precios.")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: t("desks.today.goConnections") });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[0]!);
    await waitFor(() => expect(document.querySelector('[data-screen="connections"]')).not.toBeNull());
  });

  it("every empty block says why and has a button", async () => {
    stub();
    renderShell();
    await openFund();
    await screen.findByText(t("desks.today.watch.empty.title"));
    for (const key of ["cal", "news"] as const) expect(screen.getByText(t(`desks.today.${key}.empty.title`))).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: t("desks.today.adjust") }).length).toBeGreaterThanOrEqual(4);
  });

  it("with no briefing today says so, shows the last one and generates it now", async () => {
    const old = new Date(Date.now() - 3 * 86400000).toISOString();
    const calls = stub({ today: () => json(today({ briefing: { text: "CPI in line", task_id: 4, as_of: old } })), job: { id: 9, approved: true } });
    renderShell();
    await openFund();
    expect(await screen.findByText(t("desks.today.brief.none.title"))).toBeInTheDocument();
    expect(screen.getByText(/CPI in line/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("desks.today.brief.generate") }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/scheduled-jobs/9/run" && c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.url === "/api/today/config")?.body).toMatchObject({ config: { briefing_job_id: 9 } });
    expect(screen.getByRole("button", { name: t("desks.today.brief.generating") })).toBeDisabled();
  });

  it("a briefing from today is shown as it is; a routine nobody switched on is not run, the owner is sent to Tasks", async () => {
    stub({ today: () => json(today({ briefing: { text: "Session shaped by jobs data.", task_id: 5, as_of: now } })) });
    renderShell();
    await openFund();
    expect(await screen.findByText("Session shaped by jobs data.")).toBeInTheDocument();
    expect(screen.getByText(t("desks.today.brief.today"))).toBeInTheDocument();
    cleanup();
    vi.unstubAllGlobals();
    resetDesksStore();
    const calls = stub({ job: { id: 9, approved: false } });
    renderShell();
    await openFund();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.today.brief.generate") }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/scheduled-jobs/template/premarket-briefing")).toBe(true));
    expect(calls.some((c) => c.url.endsWith("/run"))).toBe(false);
    expect(await screen.findByText(t("desks.today.brief.needsApproval"))).toBeInTheDocument();
  });

  it("'Adjust with Hokma' opens a chat that asks to change the card", async () => {
    stub();
    renderShell();
    await openFund();
    await userEvent.click((await screen.findAllByRole("button", { name: t("desks.today.adjust") }))[0]!);
    expect(await screen.findByText(t("desks.today.hokma.toast"))).toBeInTheDocument();
  });

  it("reads in English and has the same keys in both languages without a long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("desks.today.") || k.startsWith("desks.broker.") || k.startsWith("desks.kpi."));
    expect(keys.length).toBeGreaterThan(60);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(`${es[key]}${en[key]}`.includes(String.fromCharCode(0x2014)), key).toBe(false);
      expect(`${es[key]}${en[key]}`, key).not.toMatch(/paso \d|step \d/i);
    }
  });
});

describe("the Fund without desks and the Broker tab", () => {
  it("shows the Today card anyway, one single 'Crear mesa' on top and a different button in the message", async () => {
    stub();
    renderShell();
    await openFund();
    expect(await screen.findByText(t("desks.fund.empty.title"))).toBeInTheDocument();
    expect(screen.getByRole("region", { name: t("desks.today.title") })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: t("desks.create.button") })).toHaveLength(1);
    expect(screen.getByRole("button", { name: t("desks.fund.empty.button") })).toBeInTheDocument();
  });

  it("has the Broker tab next to Fund, which swaps the body and the title", async () => {
    stub();
    renderShell();
    await openFund();
    const tabs = screen.getByRole("tablist", { name: t("desks.fund.sections") });
    expect(within(tabs).getAllByRole("tab").map((x) => x.textContent)).toEqual([t("desks.fund.tab"), t("desks.fund.broker"), t("desks.fund.lessons")]);
    await userEvent.click(within(tabs).getByRole("tab", { name: t("desks.fund.broker") }));
    expect(await screen.findByRole("heading", { name: t("desks.fund.broker") })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: t("desks.today.title") })).not.toBeInTheDocument();
  });
});
