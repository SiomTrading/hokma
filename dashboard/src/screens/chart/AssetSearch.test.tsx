import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { AssetSearch, AssetSearchPopover } from "./AssetSearch";
import { RECENT_KEY, loadRecent } from "./favorites";
import type { AssetHit } from "./types";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const requests: URL[] = [];

const BTC: AssetHit = { symbol: "BTC-USD", name: "Bitcoin USD", kind: "crypto", exchange: "Coinbase", asset_class: "crypto", route: { provider: "coinbase", source: "Coinbase", data_state: "live" } };
const AAPL: AssetHit = { symbol: "AAPL", name: "Apple Inc.", kind: "equity", exchange: "NASDAQ", asset_class: "equity", route: { provider: "yahoo", source: "Yahoo Finance", data_state: "delayed" } };
const NQ: AssetHit = { symbol: "NQ=F", name: "E-mini Nasdaq 100", kind: "future", exchange: "CME", asset_class: "future", route: { provider: "yahoo", source: "Yahoo Finance", data_state: "delayed" } };
const ES: AssetHit = { symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" }; // an engine that does not send route yet

function serve() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    requests.push(url);
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const cls = url.searchParams.get("class") ?? "";
    let items: AssetHit[];
    if (q === "") items = [NQ, ES, AAPL, BTC];
    else if (q === "btc") items = [BTC];
    else if (q === "aapl") items = [AAPL];
    else items = [];
    if (cls) items = items.filter((item) => item.asset_class === cls);
    return json({ items, suggested: q === "", live_classes: ["crypto"] });
  }));
}

function Host({ initialQuery, picked }: { initialQuery?: string; picked: string[] }) {
  const [open, setOpen] = useState(true);
  const body = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={body} tabIndex={0} data-testid="chart-body">
        chart
      </div>
      {open ? <AssetSearchPopover initialQuery={initialQuery} returnFocusRef={body} onPick={(s) => picked.push(s)} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}

function show(initialQuery?: string) {
  const picked: string[] = [];
  renderWithApp(<Host initialQuery={initialQuery} picked={picked} />);
  return picked;
}

const box = () => screen.getByRole("combobox", { name: t("chart.search.label") });
const optionNames = () => within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.querySelector(".mono")?.textContent);

beforeEach(() => {
  setLocale("en");
  setToken(TEST_TOKEN);
  requests.length = 0;
  serve();
});

afterEach(() => vi.unstubAllGlobals());

describe("symbol search of the chart title (T-10 UI)", () => {
  it("shows the source of each result: live in green, delayed in amber, and nothing when the engine does not say it", async () => {
    show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    const btc = screen.getByRole("option", { name: /BTC-USD/ });
    expect(within(btc).getByTestId("route-pill")).toHaveTextContent("Live · Coinbase");
    expect(within(btc).getByTestId("route-pill")).toHaveClass("ok");
    const aapl = screen.getByRole("option", { name: /AAPL/ });
    expect(within(aapl).getByTestId("route-pill")).toHaveTextContent("Delayed · Yahoo Finance");
    expect(within(aapl).getByTestId("route-pill")).toHaveClass("warn");
    expect(within(screen.getByRole("option", { name: /ES=F/ })).queryByTestId("route-pill")).toBeNull();
  });

  it("filters by class chip, sends class, and marks the classes with a live source", async () => {
    show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    const chips = screen.getByRole("tablist", { name: "Asset class" });
    expect(within(chips).getAllByRole("tab").map((c) => c.textContent)).toEqual(["All", "Futures", "Stocks", "Crypto", "Forex", "Indices"]);
    expect(within(chips).getByRole("tab", { name: "Crypto" })).toHaveAttribute("data-live", "true");
    expect(within(chips).getByRole("tab", { name: "Stocks" })).not.toHaveAttribute("data-live");
    await userEvent.click(within(chips).getByRole("tab", { name: "Crypto" }));
    await waitFor(() => expect(optionNames()).toEqual(["BTC-USD"]));
    expect(requests.at(-1)!.searchParams.get("class")).toBe("crypto");
    expect(box()).toHaveFocus();
    await userEvent.click(within(chips).getByRole("tab", { name: "All" }));
    await waitFor(() => expect(optionNames()).toHaveLength(4));
    expect(requests.at(-1)!.searchParams.has("class")).toBe(false);
  });

  it("lists the recent symbols first while the field is empty", async () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify(["AAPL", "CL=F"]));
    show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    expect(screen.getByRole("group", { name: "Recent" })).toBeInTheDocument();
    expect(optionNames()).toEqual(["AAPL", "CL=F", "NQ=F", "ES=F", "BTC-USD"]);
  });

  it("opens with the letter typed on the chart and searches it", async () => {
    show("b");
    expect(box()).toHaveValue("b");
    expect(box()).toHaveFocus();
    await userEvent.type(box(), "tc");
    await waitFor(() => expect(optionNames()).toEqual(["BTC-USD"]));
    expect(requests.at(-1)!.searchParams.get("q")).toBe("btc");
  });

  it("moves with the arrows, picks with Enter, remembers the pick and gives the focus back (T-12)", async () => {
    const picked = show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    const second = screen.getByRole("option", { name: /ES=F/ });
    expect(second).toHaveAttribute("aria-selected", "true");
    expect(box()).toHaveAttribute("aria-activedescendant", second.id);
    await userEvent.keyboard("{ArrowUp}{ArrowUp}"); // wraps to the last one
    expect(screen.getByRole("option", { name: /BTC-USD/ })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{Enter}");
    expect(picked).toEqual(["BTC-USD"]);
    expect(loadRecent()).toEqual(["BTC-USD"]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("chart-body")).toHaveFocus();
  });

  it("changes the chip with Tab and Shift+Tab, keeping the focus in the field", async () => {
    show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    const chip = (name: string) => screen.getByRole("tab", { name });
    await userEvent.tab();
    expect(chip("Futures")).toHaveAttribute("aria-selected", "true");
    expect(box()).toHaveFocus();
    await userEvent.tab({ shift: true });
    await userEvent.tab({ shift: true });
    expect(chip("Indices")).toHaveAttribute("aria-selected", "true");
    expect(requests.at(-1)!.searchParams.get("class")).toBe("index");
  });

  it("closes with Esc and gives the focus back to the chart", async () => {
    const picked = show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("chart-body")).toHaveFocus();
    expect(picked).toEqual([]);
  });

  it("still opens a typed ticker with Enter when nothing is highlighted", async () => {
    const picked = show();
    await screen.findByRole("option", { name: /BTC-USD/ });
    await userEvent.type(box(), "cl=f{Enter}");
    expect(picked).toEqual(["CL=F"]);
  });
});

describe("the search of the empty chart and of the + menu says the source too (task 3081)", () => {
  it("shows the source pill on every result, as the popover does, and keeps the kind", async () => {
    renderWithApp(<AssetSearch compact onPick={() => {}} />);
    const aapl = await screen.findByRole("menuitem", { name: /AAPL/ });
    expect(within(aapl).getByTestId("route-pill")).toHaveTextContent("Delayed · Yahoo Finance");
    expect(within(aapl).getByTestId("route-pill")).toHaveClass("warn");
    expect(aapl).toHaveTextContent(t("chart.kind.equity"));
    const btc = screen.getByRole("menuitem", { name: /BTC-USD/ });
    expect(within(btc).getByTestId("route-pill")).toHaveTextContent("Live · Coinbase");
    expect(within(btc).getByTestId("route-pill")).toHaveClass("ok");
    expect(within(screen.getByRole("menuitem", { name: /ES=F/ })).queryByTestId("route-pill")).toBeNull();
  });

  it("says Live · Finnhub for a stock the engine routes to Finnhub", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [{ ...AAPL, route: { provider: "finnhub", source: "Finnhub", data_state: "live" } }], suggested: false, live_classes: ["equity"] })));
    renderWithApp(<AssetSearch onPick={() => {}} />);
    const aapl = await screen.findByRole("menuitem", { name: /AAPL/ });
    expect(within(aapl).getByTestId("route-pill")).toHaveTextContent("Live · Finnhub");
  });
});
