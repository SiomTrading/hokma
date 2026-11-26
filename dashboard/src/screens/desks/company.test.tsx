import { useState } from "react";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { CompanyTab } from "./tabs/CompanyTab";
import type { DeskSummary } from "./types";

const DESK = {
  id: 4, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
} as DeskSummary;

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const FULL = {
  symbol: "AAPL",
  profile: { ok: true, source: "Finnhub", name: "Apple Inc", exchange: "NASDAQ", industry: "Technology", currency: "USD", market_cap_millions: 3500000 },
  fundamentals: { ok: true, source: "Finnhub", ratios: { market_cap_millions: 3600000, beta: 1.24, pe: 36.9, eps: 6.4, operating_margin: 31.5, debt_to_equity: 1.5, revenue_growth: 6.1 } },
  events: {
    earnings: { ok: true, source: "Finnhub", next: { date: "2999-01-30", hour: "amc", eps_estimate: 2.1 } },
    expirations: { ok: true, source: "Schwab", items: [{ date: "2026-12-18", days: 23 }, { date: "2027-01-15", days: 51 }] },
  },
  results: { ok: true, source: "Finnhub", rows: [{ period: "2026-09-30", year: 2026, quarter: 3, eps_estimate: 1.5, eps_actual: 1.6, surprise_pct: 6.7 }] },
  news: { ok: true, source: "Finnhub", items: [{ time: "2026-11-24T10:00:00+00:00", headline: "Apple lanza algo", source: "Reuters", url: "https://example.com/a" }] },
};

function stubApi(company: unknown, assets = ["AAPL", "MSFT"]) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      if (String(url).includes("/company/")) return json(company);
      return json({ id: 4, name: "Mesa A", assets: assets.map((symbol) => ({ symbol, note: null })), connections: [] });
    }),
  );
  return calls;
}

const block = (title: string) => screen.getByRole("heading", { name: title }).closest("section") as HTMLElement;

beforeEach(() => {
  setLocale("es");
  clearToken();
  setToken(TEST_TOKEN);
  localStorage.clear();
  installFakeSockets();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Company panel", () => {
  it("P5: shows the five blocks as tables, from the endpoint of the desk", async () => {
    const calls = stubApi(FULL);
    renderWithApp(<CompanyTab desk={DESK} />);
    expect(await screen.findByText("Apple lanza algo")).toBeInTheDocument();
    expect(calls).toContain("/api/desks/4/company/AAPL");
    for (const title of ["quote", "fundamentals", "events", "results", "news"]) expect(block(t("desks.company." + title)).querySelector("table")).not.toBeNull();
    const fundamentals = within(block(t("desks.company.fundamentals")));
    expect(fundamentals.getByText("36,9")).toBeInTheDocument();
    expect(fundamentals.getByText("31,5%")).toBeInTheDocument();
    expect(within(block(t("desks.company.events"))).getByText(/EPS estimado 2,1/)).toBeInTheDocument();
    expect(within(block(t("desks.company.events"))).getAllByRole("row").length).toBeGreaterThan(3);
    expect(within(block(t("desks.company.results"))).getByText("+6,70%")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Apple lanza algo" })).toHaveAttribute("href", "https://example.com/a");
  });

  it("P5: the price moves with the stream and no timer asks for it", async () => {
    stubApi(FULL);
    renderWithApp(<CompanyTab desk={DESK} />);
    await screen.findByText("Apple lanza algo");
    await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(0));
    const socket = FakeSocket.of("/ws/market")[0]!;
    act(() => socket.serverOpen());
    const sub = String(socket.ops("subscribe")[0]!.sub);
    act(() => socket.serverSend({ type: "snapshot", sub, seq: 1, epoch: "e1", last: 230.5, previous_close: 228, change_pct: 1.1, quote: { volume: 52_000_000 }, source: "Schwab", data_state: "live" }));
    const quote = within(block(t("desks.company.quote")));
    expect(await quote.findByText("230,50")).toBeInTheDocument();
    expect(quote.getByText("+1,10%")).toBeInTheDocument();
    expect(quote.getByText(/52/)).toBeInTheDocument();
    act(() => socket.serverSend({ type: "last", sub, seq: 2, epoch: "e1", price: 231.2, change_pct: 1.4, source: "Schwab", data_state: "live" }));
    expect(await quote.findByText("231,20")).toBeInTheDocument();
  });

  it("P5: a provider that is down says 'Sin dato de ...' in its block and never a zero", async () => {
    stubApi({
      ...FULL,
      fundamentals: { ok: false, source: "Finnhub", reason: "Finnhub no respondio." },
      results: { ok: false, source: "Finnhub", reason: "sin plan" },
    });
    renderWithApp(<CompanyTab desk={DESK} />);
    expect(await screen.findByText("Apple lanza algo")).toBeInTheDocument();
    expect(within(block(t("desks.company.fundamentals"))).getByText(t("desks.company.noData", { source: "Finnhub" }))).toBeInTheDocument();
    expect(block(t("desks.company.fundamentals")).querySelector("table")).toBeNull();
    expect(within(block(t("desks.company.results"))).getByText(t("desks.company.noData", { source: "Finnhub" }))).toBeInTheDocument();
    expect(within(block(t("desks.company.news"))).queryByText(/Sin dato/)).not.toBeInTheDocument();      // one block down does not take the rest
  });

  it("a number the provider does not give is a dash, not a zero", async () => {
    stubApi({ ...FULL, fundamentals: { ok: true, source: "Finnhub", ratios: { market_cap_millions: null, beta: null, pe: null, eps: null, operating_margin: null, debt_to_equity: null, revenue_growth: null } } });
    renderWithApp(<CompanyTab desk={DESK} />);
    await screen.findByText("Apple lanza algo");
    const cells = within(block(t("desks.company.fundamentals"))).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["-", "-", "-", "-", "-", "-", "-"]);
  });

  it("picks another asset of the desk and asks for it", async () => {
    const calls = stubApi(FULL);
    renderWithApp(<CompanyTab desk={DESK} />);
    await screen.findByText("Apple lanza algo");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.company.asset") }), "MSFT");
    await waitFor(() => expect(calls).toContain("/api/desks/4/company/MSFT"));
  });

  it("a ticker asked from another panel is used once: asking for the same one again goes back to it", async () => {
    const calls = stubApi(FULL);
    function Harness() {
      const [focus, setFocus] = useState<string | null>(null);
      return (
        <>
          <button onClick={() => setFocus("MSFT")}>go</button>
          <CompanyTab desk={DESK} focusSymbol={focus} onFocusUsed={() => setFocus(null)} />
        </>
      );
    }
    renderWithApp(<Harness />);
    await screen.findByText("Apple lanza algo");
    const pick = screen.getByRole("combobox", { name: t("desks.company.asset") }) as HTMLSelectElement;
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(pick.value).toBe("MSFT"));
    await userEvent.selectOptions(pick, "AAPL");
    await waitFor(() => expect(pick.value).toBe("AAPL"));
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(pick.value).toBe("MSFT"));
    expect(calls.filter((c) => c === "/api/desks/4/company/MSFT").length).toBeGreaterThan(0);
  });

  it("a desk without assets says what to do", async () => {
    stubApi(FULL, []);
    renderWithApp(<CompanyTab desk={DESK} />);
    expect(await screen.findByText(t("desks.company.empty.title"))).toBeInTheDocument();
  });
});
