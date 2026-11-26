import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { DeskScreen } from "./DeskScreen";
import { addDays, CalendarTab, mondayOf } from "./tabs/CalendarTab";
import { resetDesksStore } from "./store";
import type { DeskSummary } from "./types";

const DESK = {
  id: 4, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
} as DeskSummary;

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

function row(symbol: string, date: string, patch: Record<string, unknown> = {}) {
  return { symbol, date, hour: "amc", eps_estimate: 1.5, eps_actual: null, surprise_pct: null, in_list: false, ...patch };
}

function week(monday: string, rows: Record<number, unknown[]> = {}, patch: Record<string, unknown> = {}) {
  return {
    from: monday, to: addDays(monday, 4), filter: "all", today: monday, connected: true, source: "Finnhub", holidays_known: true, liquid: null,
    days: [0, 1, 2, 3, 4].map((n) => ({ date: addDays(monday, n), holiday: n === 3 ? true : false, truncated: false, error: null, rows: rows[n] ?? [] })),
    ...patch,
  };
}

/** The Monday of this week as the screen computes it, so the stub answers the week it asks for. */
const days = () => screen.getAllByRole("region").filter((r) => r.classList.contains("calendar-day"));

const THIS_MONDAY = (() => {
  const now = new Date();
  return mondayOf(new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())).toISOString().slice(0, 10));
})();

function stubApi(answer: (url: string) => unknown, assets = ["AAPL"]) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(String(url));
      if (String(url).includes("/panels")) return json({ panels: ["terminal", "calendar", "company", "agents", "operations", "rules", "config"], method: init?.method });
      if (String(url).includes("/earnings-calendar")) return json(answer(String(url)));
      if (String(url).includes("/company/")) return json({ symbol: "TSLA", profile: { ok: false, source: "X", reason: "r" }, fundamentals: { ok: false, source: "X", reason: "r" }, events: { earnings: { ok: false, source: "X", reason: "r" }, expirations: { ok: false, source: "X", reason: "r" } }, results: { ok: false, source: "X", reason: "r" }, news: { ok: false, source: "X", reason: "r" } });
      return json({ id: 4, name: "Mesa A", assets: assets.map((symbol) => ({ symbol, note: null })), connections: [], panels: ["terminal", "calendar", "agents", "operations", "rules", "config"] });
    }),
  );
  return calls;
}

beforeEach(() => {
  setLocale("es");
  clearToken();
  setToken(TEST_TOKEN);
  localStorage.clear();
  resetDesksStore();
  installFakeSockets();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Calendar panel", () => {
  it("C1: the week is five columns, Monday to Friday, with who reports, when and the estimate; today is marked and the holiday too", async () => {
    const calls = stubApi(() => week(THIS_MONDAY, { 0: [row("AAPL", THIS_MONDAY, { hour: "bmo" })], 2: [row("MSFT", addDays(THIS_MONDAY, 2), { eps_estimate: 2.75 })] }));
    renderWithApp(<CalendarTab desk={DESK} />);
    expect(await screen.findByRole("button", { name: t("desks.calendar.openCompany", { symbol: "AAPL" }) })).toBeInTheDocument();
    expect(calls[0]).toBe(`/api/desks/4/earnings-calendar?from=${THIS_MONDAY}&to=${addDays(THIS_MONDAY, 4)}&filter=all`);
    expect(days().length).toBe(5);
    const monday = days()[0]!;
    expect(within(monday).getByText(t("desks.calendar.hour.bmo"))).toBeInTheDocument();
    expect(within(monday).getByText(t("desks.calendar.today"))).toBeInTheDocument();
    expect(within(days()[2]!).getByText("2,75")).toBeInTheDocument();
    expect(within(days()[3]!).getByText(t("desks.calendar.holiday"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.calendar.source", { source: "Finnhub" }))).toBeInTheDocument();
  });

  it("C2: a published report shows the real EPS and the surprise, green when it beat and red when it missed", async () => {
    stubApi(() => week(THIS_MONDAY, { 0: [row("AAPL", THIS_MONDAY, { eps_actual: 1.65, surprise_pct: 10 }), row("KO", THIS_MONDAY, { eps_actual: 0.8, surprise_pct: -20 }), row("MSFT", THIS_MONDAY)] }));
    renderWithApp(<CalendarTab desk={DESK} />);
    await screen.findByRole("button", { name: t("desks.calendar.openCompany", { symbol: "AAPL" }) });
    const rows = screen.getAllByRole("row");
    const of = (symbol: string) => rows.find((r) => within(r).queryByRole("button", { name: t("desks.calendar.openCompany", { symbol }) }))!;
    expect(within(of("AAPL")).getByText("+10,00%").closest("td")).toHaveClass("up");
    expect(within(of("KO")).getByText("-20,00%").closest("td")).toHaveClass("down");
    expect(within(of("MSFT")).queryByText(/%/)).toBeNull();                                  // not published: no real number, no surprise
  });

  it("C3: the filters ask the server again and 'my list' is one of them; the other weeks are one click away", async () => {
    const calls = stubApi((url) => week(THIS_MONDAY, {}, { filter: url.includes("filter=list") ? "list" : "all" }));
    renderWithApp(<CalendarTab desk={DESK} />);
    await screen.findAllByText(t("desks.calendar.noReports"));
    await userEvent.click(screen.getByRole("button", { name: t("desks.calendar.filter.list") }));
    await waitFor(() => expect(calls.some((c) => c.endsWith("filter=list"))).toBe(true));
    expect((await screen.findAllByText(t("desks.calendar.noMatches"))).length).toBe(5);
    await userEvent.click(screen.getByRole("button", { name: t("desks.calendar.next") }));
    await waitFor(() => expect(calls.some((c) => c.includes(`from=${addDays(THIS_MONDAY, 7)}`) && c.endsWith("filter=list"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: t("desks.calendar.thisWeek") }));
    await waitFor(() => expect(calls.at(-1)).toContain(`from=${THIS_MONDAY}`));
  });

  it("the liquid filter says how many companies it could not read", async () => {
    stubApi(() => week(THIS_MONDAY, {}, { filter: "liquid", liquid: { checked: 18, unchecked: 7 } }));
    renderWithApp(<CalendarTab desk={DESK} />);
    expect(await screen.findByText(t("desks.calendar.liquidNote", { n: "18", m: "7" }))).toBeInTheDocument();
  });

  it("C4: without the data connection it asks to connect it and does not draw the week", async () => {
    stubApi(() => week(THIS_MONDAY, {}, { connected: false, days: [], source: null }));
    renderWithApp(<CalendarTab desk={DESK} />);
    expect(await screen.findByText(t("desks.calendar.connect.title"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.calendar.connect.go") })).toBeInTheDocument();
    expect(document.querySelector(".calendar-day")).toBeNull();
  });

  it("without holiday data the panel says so, and a day the source did not deliver says it is not an empty day", async () => {
    stubApi(() => {
      const w = week(THIS_MONDAY, {}, { holidays_known: false }) as { days: Array<Record<string, unknown>> };
      w.days[1] = { ...w.days[1]!, error: "plan", rows: [] };
      w.days[2] = { ...w.days[2]!, truncated: true, rows: [row("AAPL", String(w.days[2]!.date))] };
      return w;
    });
    renderWithApp(<CalendarTab desk={DESK} />);
    expect(await screen.findByText(t("desks.calendar.noHolidays"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.calendar.dayError"))).toBeInTheDocument();
    expect(screen.getByText(t("desks.calendar.truncated"))).toBeInTheDocument();
  });

  it("a server error shows a retry and no numbers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: { code: "x", message: "Fallo" } }, 500)));
    renderWithApp(<CalendarTab desk={DESK} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Fallo");
    expect(screen.getByRole("button", { name: t("ui.error.retry") })).toBeInTheDocument();
    expect(document.querySelector(".calendar-day")).toBeNull();
  });

  it("no timer asks for the calendar and nothing is pushed by the stream", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const calls = stubApi(() => week(THIS_MONDAY));
      renderWithApp(<CalendarTab desk={DESK} />);
      await screen.findAllByText(t("desks.calendar.noReports"));
      vi.advanceTimersByTime(10 * 60 * 1000);
      expect(calls.filter((c) => c.includes("/earnings-calendar")).length).toBe(1);
      expect(FakeSocket.of("/ws/market").length).toBe(0);                                  // no price is shown here, so no subscription
    } finally {
      vi.useRealTimers();
    }
  });

  it("C5: a click on a ticker opens Company with that asset, even when it is not one of the desk's", async () => {
    const calls = stubApi(() => week(THIS_MONDAY, { 1: [row("TSLA", addDays(THIS_MONDAY, 1))] }));
    const desk = { ...DESK, panels: ["terminal", "calendar", "agents", "operations", "rules", "config"] } as DeskSummary;
    renderWithApp(<DeskScreen desk={desk} desks={[desk]} tabKey="k" initialTab="calendar" />);
    await userEvent.click(await screen.findByRole("button", { name: t("desks.calendar.openCompany", { symbol: "TSLA" }) }));
    await waitFor(() => expect(calls).toContain("/api/desks/4/company/TSLA"));
    expect(screen.getByRole("tab", { name: t("desks.tab.company") })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: t("desks.company.asset") })).toHaveValue("TSLA");
  });
});

describe("week arithmetic", () => {
  it("finds the Monday of any day, across a month and a year", () => {
    expect(mondayOf("2026-11-25")).toBe("2026-11-23");
    expect(mondayOf("2026-11-23")).toBe("2026-11-23");
    expect(mondayOf("2026-11-29")).toBe("2026-11-23");
    expect(mondayOf("2027-01-01")).toBe("2026-12-28");
    expect(addDays("2026-12-28", 4)).toBe("2027-01-01");
  });
});

describe("texts of the calendar", () => {
  it("exist in Spanish and English with the same placeholders, name no provider and carry no long dash", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((key) => key.startsWith("desks.calendar.") || key === "desks.tab.calendar" || key === "desks.panel.desc.calendar");
    expect(keys.length).toBeGreaterThan(25);
    const holes = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort().join();
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(holes(en[key]!), key).toBe(holes(es[key]!));
      expect(es[key] + en[key]).not.toContain(String.fromCharCode(0x2014));
      expect(es[key] + en[key]).not.toMatch(/finnhub|alpaca|schwab/i);
    }
  });
});
