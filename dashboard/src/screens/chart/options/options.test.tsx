import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, getToken, setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { FakeSocket, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { ShellProvider } from "@/shell/store";
import { TEST_TOKEN } from "@/test/utils";
import { useFakeCharts } from "../testChartLib";
import { mountHost, seedTabs } from "../testHost";
import { COLUMN_SET_KEY, OptionChainView, STRIKES_KEY } from "./OptionChainView";
import type { ChainResponse, ExpirationGroup, OptionCell, OptionRow } from "./types";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const UNDERLYING = {
  symbol: "AAPL", last: 32.52, last_exchange: "Q", net_change: -0.65, bid: 32.35, bid_exchange: "X", ask: 32.91, ask_exchange: "D",
  bid_size: 11, ask_size: 115, volume: 731132, open: 33.71, high: 33.95, low: 32.33, as_of: "2026-11-20T20:59:59Z",
};
const EXPIRATIONS = [
  { date: "2026-11-20", days: 0, multiplier: 100 },
  { date: "2026-12-18", days: 28, multiplier: 100 },
  { date: "2027-01-15", days: 56, multiplier: 100 },
  { date: "2027-01-29", days: 70, multiplier: 100 },
  { date: "2027-02-19", days: 91, multiplier: 100 },
];

function cell(kind: "call" | "put", strike: number, over: Partial<OptionCell> = {}): OptionCell {
  const itm = kind === "call" ? strike < 32.52 : strike > 32.52;
  return {
    symbol: "AAPL261218" + (kind === "call" ? "C" : "P") + String(strike * 1000).padStart(8, "0"), itm, last: 3.7, last_exchange: null,
    bid: 5.2, bid_exchange: null, ask: 5.4, ask_exchange: null, volume: null, open_interest: 1500, theta: -0.38, delta: kind === "call" ? 0.6 : -0.6,
    iv: 0.62, intrinsic: itm ? Math.abs(32.52 - strike) : 0, prob_itm: 0.8238, quote_time: "2026-11-20T20:59:58Z", ...over,
  };
}

function group(date: string, days: number, rows?: OptionRow[]): ExpirationGroup {
  return {
    date, days, multiplier: 100, iv: 0.5969, expected_move: 4.378,
    rows: rows ?? [31, 32, 33, 34].map((strike) => ({ strike, call: cell("call", strike), put: cell("put", strike) })),
  };
}

interface Server {
  chain: (dates: string[], strikes: string) => ChainResponse | { status: number; code: string };
  expirations: () => unknown;
  underlying: () => unknown;
}

const feed = (over: Partial<ChainResponse> = {}): Omit<ChainResponse, "expirations"> => ({
  underlying: "AAPL", underlying_price: 32.52, source: "Alpaca", price_feed: "indicative", greeks: "estimated", market_open: true,
  as_of: "2026-11-20T20:59:58Z", notes: [], ...over,
});

function defaults(over: Partial<Server> = {}): Server {
  return {
    chain: (dates) => ({
      ...feed(),
      expirations: dates.map((d) => group(d, EXPIRATIONS.find((e) => e.date === d)?.days ?? 0)),
    }),
    expirations: () => ({ symbol: "AAPL", source: "Alpaca", expirations: EXPIRATIONS }),
    underlying: () => UNDERLYING,
    ...over,
  };
}

let server: Server;
let calls: URL[];

function install(over: Partial<Server> = {}) {
  server = defaults(over);
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const url = new URL(input, "http://hokma.test");
      calls.push(url);
      if (url.pathname === "/api/market/options/underlying") return json(server.underlying());
      if (url.pathname === "/api/market/options/expirations") return json(server.expirations());
      if (url.pathname === "/api/market/options/chain") {
        const answer = server.chain(url.searchParams.get("expirations")!.split(","), url.searchParams.get("strikes")!);
        return "status" in answer ? json({ detail: { code: answer.code, message: "x" } }, answer.status) : json(answer);
      }
      return json({ items: [], suggested: true });
    }),
  );
}
const chainCalls = () => calls.filter((u) => u.pathname === "/api/market/options/chain");

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  localStorage.clear();
  setLocale("en");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setLocale("es");
  localStorage.clear();
});

const mount = () =>
  render(
    <ShellProvider>
      <OptionChainView symbol="AAPL" />
    </ShellProvider>,
  );

async function open(locale: "es" | "en" = "en") {
  setLocale(locale);
  const view = mount();
  await screen.findByTestId("options-group-2026-11-20");
  await waitFor(() => expect(chainCalls().length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.queryByText(t("options.loading"))).toBeNull());
  return view;
}
const flat = (text: string | null) => (text ?? "").replace(/ /g, " ");
const headers = () => within(screen.getByRole("table")).getAllByRole("columnheader").map((h) => flat(h.textContent));
const bodyRows = () => Array.from(document.querySelectorAll<HTMLElement>("tr.opt-row"));
const strikeRow = (strike: number) => document.querySelector<HTMLElement>('tr.opt-row[data-strike="' + strike + '"]')!;
const groupButton = (date: string) => within(screen.getByTestId("options-group-" + date)).getByRole("button");

describe("the bar of the underlying", () => {
  beforeEach(() => install());

  it("shows the nine fields, with Size as bid x ask and the net change in its color", async () => {
    await open();
    const bar = screen.getByTestId("options-underlying");
    const field = (key: string) => bar.querySelector('[data-field="' + key + '"]')!;
    expect(Array.from(bar.querySelectorAll("dt")).map((n) => n.textContent)).toEqual([
      "Last X", "Net Chng", "Bid X", "Ask X", "Size", "Volume", "Open", "High", "Low",
    ]);
    expect(field("last").querySelector("dd")!.textContent).toBe("32.52 Q");
    expect(field("net").querySelector("dd")).toHaveClass("opt-down");
    expect(field("net").querySelector("dd")!.textContent).toBe("-0.65");
    expect(field("bid").querySelector("dd")!.textContent).toBe("32.35 X");
    expect(field("ask").querySelector("dd")!.textContent).toBe("32.91 D");
    expect(field("size").querySelector("dd")!.textContent).toBe("11 x 115");
    expect(field("volume").querySelector("dd")!.textContent).toBe("731,132");
    expect(["open", "high", "low"].map((k) => field(k).querySelector("dd")!.textContent)).toEqual(["33.71", "33.95", "32.33"]);
  });

  it("says N/A for what did not arrive and writes no letter beside a price without exchange", async () => {
    install({ underlying: () => ({ ...UNDERLYING, volume: null, last_exchange: null, bid_size: null, ask_size: null, net_change: 0.5 }) });
    await open();
    const bar = screen.getByTestId("options-underlying");
    expect(bar.querySelector('[data-field="volume"] dd')!.textContent).toBe("N/A");
    expect(bar.querySelector('[data-field="last"] dd')!.textContent).toBe("32.52");
    expect(bar.querySelector('[data-field="size"] dd')!.textContent).toBe("N/A");
    expect(bar.querySelector('[data-field="net"] dd')).toHaveClass("opt-up");
  });

  it("folds to its title", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Underlying/ }));
    expect(screen.queryByText("Net Chng")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Underlying/ }));
    expect(screen.getByText("Net Chng")).toBeInTheDocument();
  });
});

describe("the table", () => {
  beforeEach(() => install());

  it("has CALLS on the left, Exp and Strike in the middle and PUTS on the right, set 1 by default", async () => {
    await open();
    expect(headers()).toEqual([
      "CALLS", "PUTS",
      "Last X", "Prob.ITM", "Intrinsic", "Open.Int", "Volume", "Theta", "Bid X", "Ask X", "Exp", "Strike",
      "Bid X", "Ask X", "Last X", "Prob.ITM", "Intrinsic", "Open.Int", "Volume", "Theta",
    ]);
    expect(screen.getByTestId("options-columns")).toHaveValue("1");
  });

  it("set 2 adds Delta and IV on both sides and no set shows Gamma or Vega", async () => {
    await open();
    await userEvent.selectOptions(screen.getByTestId("options-columns"), "2");
    expect(headers().slice(2)).toEqual([
      "Last X", "Prob.ITM", "Intrinsic", "Open.Int", "Volume", "Theta", "Delta", "IV", "Bid X", "Ask X", "Exp", "Strike",
      "Bid X", "Ask X", "Delta", "IV", "Last X", "Prob.ITM", "Intrinsic", "Open.Int", "Volume", "Theta",
    ]);
    expect(localStorage.getItem(COLUMN_SET_KEY)).toBe("2");
    for (const set of ["1", "2"]) {
      await userEvent.selectOptions(screen.getByTestId("options-columns"), set);
      expect(document.body.textContent).not.toMatch(/gamma|vega/i);
    }
  });

  it("opens three groups at once with one request and loads a folded group when it is opened", async () => {
    await open();
    expect(chainCalls()).toHaveLength(1);
    expect(chainCalls()[0]!.searchParams.get("expirations")).toBe("2026-11-20,2026-12-18,2027-01-15");
    expect(chainCalls()[0]!.searchParams.get("strikes")).toBe("14");
    expect(groupButton("2027-01-29")).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(groupButton("2027-01-29"));
    await waitFor(() => expect(chainCalls()).toHaveLength(2));
    expect(chainCalls()[1]!.searchParams.get("expirations")).toBe("2027-01-29");
    await waitFor(() => expect(bodyRows().length).toBe(16));
    // folding keeps what was loaded and unfolding it again asks for nothing
    await userEvent.click(groupButton("2027-01-29"));
    expect(bodyRows().length).toBe(12);
    await userEvent.click(groupButton("2027-01-29"));
    expect(chainCalls()).toHaveLength(2);
    expect(bodyRows().length).toBe(16);
  });

  it("writes the header of a group as DD MMM YY (days) multiplier with the IV and the expected move on the right, in English", async () => {
    await open("en");
    const bar = screen.getByTestId("options-group-2026-12-18");
    expect(flat(within(bar).getByRole("button").textContent)).toBe("▾ 18 DEC 26 (28) 100");
    expect(flat(within(bar).getByTestId("options-group-summary").textContent)).toBe("59.69% (±4.378)");
  });

  it("writes the header of a group in Spanish with the Spanish number format", async () => {
    await open("es");
    const bar = screen.getByTestId("options-group-2026-12-18");
    expect(flat(within(bar).getByRole("button").textContent)).toBe("▾ 18 DIC 26 (28) 100");
    expect(flat(within(bar).getByTestId("options-group-summary").textContent)).toBe("59,69 % (±4,378)");
    expect(headers().slice(2, 4)).toEqual([t("options.col.last"), t("options.col.probItm")]);
    expect(headers()[2]).not.toBe("Last X");
  });

  it("repeats the date of the group in every row and shades the in the money cells of each side, and only those", async () => {
    await open();
    const first = strikeRow(31);
    expect(first.querySelector(".opt-center-col")!.textContent).toBe("20 NOV 26");
    const shaded = (tr: HTMLElement, side: string) =>
      Array.from(tr.querySelectorAll('td[data-side="' + side + '"]')).map((td) => td.classList.contains("opt-itm"));
    expect(shaded(first, "call")).toHaveLength(8);
    expect(shaded(first, "call").every(Boolean)).toBe(true);            // call with the strike 31 below 32.52
    expect(shaded(first, "put").some(Boolean)).toBe(false);
    expect(shaded(strikeRow(34), "put").every(Boolean)).toBe(true);
    expect(shaded(strikeRow(34), "call").some(Boolean)).toBe(false);
  });

  it("formats prices, percentages and whole numbers, writes the exchange letter only when it comes and N/A when a value is missing", async () => {
    install({
      chain: (dates) => ({
        ...feed(),
        expirations: dates.map((d) =>
          group(d, 0, [{
            strike: 32,
            call: cell("call", 32, { last: 3.7, last_exchange: "V", volume: null, open_interest: 5, bid: 0, ask: 8.6, ask_exchange: "S", prob_itm: 0.8457, theta: -0.375 }),
            put: cell("put", 32, { volume: 1234567 }),
          }]),
        ),
      }),
    });
    await open();
    const row = document.querySelector<HTMLElement>("tr.opt-row")!;
    const call = (col: string) => row.querySelector('td[data-side="call"][data-col="' + col + '"]')!.textContent;
    expect(call("last")).toBe("3.70 V");
    expect(call("probItm")).toBe("84.57%");
    expect(call("intrinsic")).toBe("0.52");
    expect(call("openInterest")).toBe("5");
    expect(call("volume")).toBe("N/A");
    expect(call("theta")).toBe("-0.38");
    expect(call("bid")).toBe("0.00");
    expect(call("ask")).toBe("8.60 S");
    expect(row.querySelector('td[data-side="put"][data-col="volume"]')!.textContent).toBe("1,234,567");
    expect(row.querySelector('td[data-side="put"][data-col="last"]')!.textContent).toBe("3.70");
  });

  it("writes the numbers in the language of the panel and leaves a side without contract empty", async () => {
    install({
      chain: (dates) => ({ ...feed(), expirations: dates.map((d) => group(d, 0, [{ strike: 32, call: cell("call", 32, { volume: 1234567, prob_itm: 0.8457 }), put: null }])) }),
    });
    await open("es");
    const row = document.querySelector<HTMLElement>("tr.opt-row")!;
    expect(flat(row.querySelector('td[data-side="call"][data-col="volume"]')!.textContent)).toBe("1.234.567");
    expect(flat(row.querySelector('td[data-side="call"][data-col="probItm"]')!.textContent)).toBe("84,57 %");
    expect(row.querySelector('td[data-side="put"]')!.textContent).toBe("");
  });

  it("changes the strikes of the request, remembers the choice and asks again for the open groups only", async () => {
    await open();
    expect(screen.getByTestId("options-strikes")).toHaveValue("14");
    await userEvent.selectOptions(screen.getByTestId("options-strikes"), "6");
    await waitFor(() => expect(chainCalls()).toHaveLength(2));
    expect(chainCalls()[1]!.searchParams.get("strikes")).toBe("6");
    expect(chainCalls()[1]!.searchParams.get("expirations")).toBe("2026-11-20,2026-12-18,2027-01-15");
    expect(localStorage.getItem(STRIKES_KEY)).toBe("6");
    await userEvent.selectOptions(screen.getByTestId("options-strikes"), "all");
    await waitFor(() => expect(chainCalls()).toHaveLength(3));
    expect(chainCalls()[2]!.searchParams.get("strikes")).toBe("all");
    expect(Array.from(screen.getByTestId("options-strikes").querySelectorAll("option")).map((o) => o.textContent)).toEqual(
      ["4", "6", "8", "10", "12", "14", "16", "20", "30", "50", "All"],
    );
  });

  it("starts with the remembered columns and strikes", async () => {
    localStorage.setItem(COLUMN_SET_KEY, "2");
    localStorage.setItem(STRIKES_KEY, "30");
    await open();
    expect(screen.getByTestId("options-columns")).toHaveValue("2");
    expect(chainCalls()[0]!.searchParams.get("strikes")).toBe("30");
  });

  it("Refresh asks again for the open groups, with refresh, and nothing refreshes by itself", async () => {
    await open();
    await userEvent.click(screen.getByTestId("options-refresh"));
    await waitFor(() => expect(chainCalls()).toHaveLength(2));
    expect(chainCalls()[1]!.searchParams.get("refresh")).toBe("true");
    expect(calls.filter((u) => u.pathname.endsWith("/underlying"))).toHaveLength(2);
  });

  it("a click on a row or a cell asks for nothing and goes nowhere", async () => {
    await open();
    const before = calls.length;
    const hash = window.location.hash;
    await userEvent.click(bodyRows()[0]!);
    await userEvent.click(bodyRows()[1]!.querySelector("td")!);
    expect(calls.length).toBe(before);
    expect(window.location.hash).toBe(hash);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("the labels under the bar", () => {
  it("says indicative prices and estimated greeks and Prob.ITM on the free plan", async () => {
    install();
    await open("en");
    expect(screen.getByTestId("options-feed")).toHaveTextContent("Estimated greeks and Prob.ITM · indicative price (free Alpaca plan)");
  });

  it("says it in Spanish", async () => {
    install();
    await open("es");
    expect(screen.getByTestId("options-feed")).toHaveTextContent("Griegas y Prob. ITM estimadas · precio indicativo (plan gratis de Alpaca)");
  });

  it("says OPRA when the account has it", async () => {
    install({ chain: (dates) => ({ ...feed({ price_feed: "opra", greeks: "calculated" }), expirations: dates.map((d) => group(d, 0)) }) });
    await open("en");
    expect(screen.getByTestId("options-feed")).toHaveTextContent("OPRA price · greeks calculated by Alpaca");
  });

  it("says the market is closed with the time of the last quote in New York", async () => {
    install({ chain: (dates) => ({ ...feed({ market_open: false, as_of: "2026-11-20T20:59:58Z" }), expirations: dates.map((d) => group(d, 0)) }) });
    await open("en");
    expect(flat(screen.getByTestId("options-closed").textContent)).toBe("Market closed · data from Nov 20, 3:59 PM");
  });

  it("writes nothing about a closed market while it is open", async () => {
    install();
    await open("en");
    expect(screen.getByTestId("options-closed")).toBeEmptyDOMElement();
  });
});

describe("the filter", () => {
  /** Rows that pass and rows that do not, in every group. */
  function pead(): Server {
    const passes = (kind: "call" | "put", strike: number) =>
      cell(kind, strike, { delta: kind === "call" ? 0.6 : -0.6, open_interest: 900, bid: 2.0, ask: 2.05 });
    return defaults({
      chain: (dates) => ({
        ...feed(),
        expirations: dates.map((d) =>
          group(d, EXPIRATIONS.find((e) => e.date === d)!.days, [
            { strike: 30, call: passes("call", 30), put: cell("put", 30, { delta: -0.2 }) },                          // only the call passes
            { strike: 31, call: cell("call", 31, { delta: 0.9 }), put: cell("put", 31, { delta: -0.2 }) },             // neither side
            { strike: 32, call: cell("call", 32, { open_interest: 100 }), put: passes("put", 32) },                    // only the put passes
            { strike: 33, call: cell("call", 33, { bid: 1, ask: 1.5 }), put: cell("put", 33, { bid: 0, ask: 1 }) },    // spread too wide, bid 0
          ]),
        ),
      }),
    });
  }

  it("is Off at first and shows the PEAD rule in the panel", async () => {
    install();
    await open();
    expect(screen.getByTestId("options-filter")).toHaveTextContent("Filter: Off");
    await userEvent.click(screen.getByTestId("options-filter"));
    const panel = screen.getByTestId("options-filter-panel");
    const value = (label: string) => (within(panel).getByLabelText(label) as HTMLInputElement).value;
    expect(["Min days", "Max days", "Min delta (absolute)", "Max delta (absolute)", "Min open interest", "Max spread %"].map(value)).toEqual(
      ["60", "90", "0.5", "0.7", "500", "5"],
    );
    expect(screen.getByRole("button", { name: "Clear filter" })).toBeDisabled();
  });

  it("hides the groups outside the days, empties the side that does not pass and hides the rows where neither passes", async () => {
    install({ chain: pead().chain });
    await open();
    expect(bodyRows().length).toBe(12);
    await userEvent.click(screen.getByTestId("options-filter"));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByTestId("options-filter")).toHaveTextContent("Filter: On");
    expect(screen.queryByTestId("options-group-2026-11-20")).toBeNull();                         // 0 days
    expect(screen.queryByTestId("options-group-2026-12-18")).toBeNull();                         // 28 days
    expect(screen.queryByTestId("options-group-2027-01-15")).toBeNull();                         // 56 days
    expect(screen.getByTestId("options-group-2027-01-29")).toBeInTheDocument();                  // 70 days
    expect(screen.queryByTestId("options-group-2027-02-19")).toBeNull();                         // 91 days
    await userEvent.click(groupButton("2027-01-29"));
    await waitFor(() => expect(bodyRows().length).toBe(2));
    const [first, second] = bodyRows();
    expect(first!.dataset.strike).toBe("30");
    expect(first!.querySelector('td[data-side="call"][data-col="last"]')!.textContent).not.toBe("");
    expect(first!.querySelector('td[data-side="put"][data-col="last"]')!.textContent).toBe("");
    expect(second!.dataset.strike).toBe("32");
    expect(second!.querySelector('td[data-side="call"][data-col="last"]')!.textContent).toBe("");
    expect(second!.querySelector('td[data-side="put"][data-col="last"]')!.textContent).not.toBe("");
  });

  it("asks for nothing when applied and Clear brings everything back", async () => {
    install({ chain: pead().chain });
    await open();
    const before = calls.length;
    await userEvent.click(screen.getByTestId("options-filter"));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(calls.length).toBe(before);
    await userEvent.click(screen.getByTestId("options-filter"));
    await userEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(screen.getByTestId("options-filter")).toHaveTextContent("Filter: Off");
    expect(screen.getByTestId("options-group-2026-11-20")).toBeInTheDocument();
    expect(bodyRows().length).toBe(12);
    expect(calls.length).toBe(before);
  });

  it("does not apply a rule that is not a rule", async () => {
    install();
    await open();
    await userEvent.click(screen.getByTestId("options-filter"));
    const min = screen.getByLabelText("Min days");
    await userEvent.clear(min);
    await userEvent.type(min, "95");
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    await userEvent.clear(min);
    await userEvent.type(min, "60");
    expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
  });
});

describe("states", () => {
  it("says there are no contracts for a group that has none", async () => {
    install({
      chain: (dates) => ({ ...feed(), expirations: dates.map((d) => (d === "2027-01-15" ? group(d, 56, []) : group(d, 0))) }),
    });
    await open();
    expect(screen.getByText("No contracts for AAPL on this expiration.")).toBeInTheDocument();
  });

  it("with no Alpaca connection the area says so and offers Connections; the bar of the underlying still shows", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const url = new URL(input, "http://hokma.test");
        if (url.pathname.endsWith("/underlying")) return json(UNDERLYING);
        return json({ detail: { code: "options.no_connection", message: "x" } }, 409);
      }),
    );
    mount();
    expect(await screen.findByText("Connect Alpaca IEX in Connections to see the option chain.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Connections" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("options-underlying").textContent).toContain("32.52"));
    expect(screen.queryByRole("table")).toBeNull();
  });

  it.each([
    ["auth_failed", 401, "Alpaca did not accept the saved key. Check it in Connections."],
    ["rate_limited", 429, "Alpaca is limiting requests. Wait a minute and try again."],
    ["upstream", 502, "The option chain could not be loaded from Alpaca. Try again later."],
    ["not_equity", 422, "Options are available for US stocks and ETFs."],
    ["bad_request", 422, "That request is not valid."],
  ])("says what %s means when the expirations cannot be read", async (code, status, text) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const url = new URL(input, "http://hokma.test");
        if (url.pathname.endsWith("/underlying")) return json(UNDERLYING);
        return json({ detail: { code: "options." + code, message: "x" } }, status);
      }),
    );
    mount();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(getToken()).toBe(TEST_TOKEN);                              // a refused key is not a refused session
  });

  it("a group that fails says why and the other groups stay", async () => {
    install({
      chain: (dates) => (dates.length > 1 ? { status: 502, code: "options.upstream" } : { ...feed(), expirations: dates.map((d) => group(d, 0)) }),
    });
    mount();
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBe(3));
    expect(screen.getAllByText("The option chain could not be loaded from Alpaca. Try again later.").length).toBe(3);
    expect(screen.getByTestId("options-group-2027-01-29")).toBeInTheDocument();
  });
});

describe("the Options tab of the chart", () => {
  beforeEach(() => {
    useFakeCharts();
    installFakeSockets();
    install();
    setLocale("es");
  });

  async function chartOf(kind: string, symbol: string) {
    seedTabs([{ key: "chart:symbol=" + symbol, params: { symbol } }]);
    mountHost();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
    act(() => socket.serverSend(snapshot(sub, 1, { symbol, kind })));
    await screen.findByTestId("chart-title");
  }

  it.each([["equity", "AAPL"], ["etf", "SPY"]])("shows Chart | Options for a %s", async (kind, symbol) => {
    await chartOf(kind, symbol);
    const tabs = await screen.findByRole("tablist", { name: t("options.tab.label") });
    expect(within(tabs).getAllByRole("tab").map((x) => x.textContent)).toEqual([t("options.tab.chart"), t("options.tab.options")]);
    expect(within(tabs).getAllByRole("tab")[0]!.textContent).not.toBe("Chart");
    expect(screen.getByTestId("view-chart")).toHaveAttribute("aria-selected", "true");
  });

  it.each([["future", "NQ=F"], ["currency", "EURUSD=X"], ["index", "^SPX"], ["crypto", "BTC-USD"], ["other", "ZZ"]])(
    "does not show Options for a %s",
    async (kind, symbol) => {
      await chartOf(kind, symbol);
      expect(screen.queryByTestId("view-options")).toBeNull();
      expect(screen.queryByRole("tablist", { name: t("options.tab.label") })).toBeNull();
    },
  );

  it("opens the chain in the same screen and goes back to the chart", async () => {
    await chartOf("equity", "AAPL");
    fireEvent.click(await screen.findByTestId("view-options"));
    expect(await screen.findByTestId("options-view")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("chart.interval.more") })).toBeNull();
    fireEvent.click(screen.getByTestId("view-chart"));
    expect(screen.queryByTestId("options-view")).toBeNull();
    expect(screen.getByRole("button", { name: t("chart.interval.more") })).toBeInTheDocument();
  });

  it("with no connection the button opens Connections", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string) => {
        const url = new URL(input, "http://hokma.test");
        if (url.pathname.startsWith("/api/market/options")) return json({ detail: { code: "options.no_connection", message: "x" } }, 409);
        return json({ items: [], suggested: true });
      }),
    );
    await chartOf("equity", "AAPL");
    fireEvent.click(await screen.findByTestId("view-options"));
    fireEvent.click(await screen.findByRole("button", { name: t("options.openConnections") }));
    await waitFor(() => expect(screen.getByLabelText("tab-keys").textContent).toContain("connections"));
  });
});

describe("texts", () => {
  it("every options key exists in both languages and none has a long dash or a forbidden word", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("options."));
    expect(keys.length).toBeGreaterThan(60);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key] + en[key], key).not.toMatch(/—/);
      expect(es[key] + en[key], key).not.toMatch(/simulad|did.ctic|juguete|simulated|toy/i);
    }
  });
});
