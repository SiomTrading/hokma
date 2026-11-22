import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale } from "@/i18n";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { OptionChainView } from "./OptionChainView";
import { applyChanges } from "./optionsLive";
import type { ExpirationGroup, OptionCell } from "./types";

function cell(symbol: string, over: Partial<OptionCell> = {}): OptionCell {
  return { symbol, itm: false, last: 1, last_exchange: null, bid: 0.9, bid_exchange: null, ask: 1.1, ask_exchange: null, volume: 10, open_interest: 100,
           theta: -0.1, delta: 0.5, iv: 0.2, intrinsic: 0, prob_itm: 0.5, quote_time: null, ...over };
}
const GROUP: ExpirationGroup = { date: "2026-12-18", days: 27, multiplier: 100, iv: 0.2, expected_move: 5,
  rows: [{ strike: 500, call: cell("C500"), put: cell("P500") }, { strike: 501, call: cell("C501"), put: cell("P501") }] };

describe("applyChanges", () => {
  it("puts each change in its cell as a new object and leaves the rest as it was", () => {
    const groups = { "2026-12-18": { status: "ready" as const, data: GROUP }, other: { status: "ready" as const, data: { ...GROUP, date: "x", rows: [] } } };
    const next = applyChanges(groups, [{ symbol: "C501", bid: 2.5, ask: 2.6, live: true }, { symbol: "NOPE", bid: 9 }]);
    expect(next["2026-12-18"]!.data!.rows[1]!.call).toMatchObject({ symbol: "C501", bid: 2.5, ask: 2.6, live: true, volume: 10 });
    expect(next["2026-12-18"]!.data!.rows[0]!.call).toBe(GROUP.rows[0]!.call);
    expect(next.other).toBe(groups.other);
    expect(GROUP.rows[1]!.call!.bid).toBe(0.9);                         // the old group was not touched
  });
});

let MARKET_OPEN: boolean | null = true;

describe("the live option chain", () => {
  beforeEach(() => {
    clearToken();
    setToken(TEST_TOKEN);
    setLocale("en");
    installFakeSockets();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      if (url.includes("/options/expirations")) return json({ symbol: "QQQ", source: "Schwab", expirations: [{ date: "2026-12-18", days: 27, multiplier: 100 }] });
      if (url.includes("/options/underlying")) return json({ symbol: "QQQ", last: 500.4, last_exchange: null, net_change: 1, bid: 500.3, bid_exchange: null, ask: 500.5, ask_exchange: null,
        bid_size: 1, ask_size: 1, volume: 10, open: 499, high: 501, low: 498, as_of: null });
      if (url.includes("/options/chain")) return json({ underlying: "QQQ", underlying_price: 500.4, source: "Schwab", price_feed: "schwab", greeks: "provider", market_open: MARKET_OPEN,
        as_of: "2026-11-20T15:00:00Z", notes: [], expirations: [GROUP] });
      return new Response("{}", { status: 404 });
    }));
  });
  afterEach(() => {
    MARKET_OPEN = true;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function opened() {
    renderWithApp(<OptionChainView symbol="QQQ" />);
    await screen.findByTestId("options-group-2026-12-18");
    await vi.waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(0));
    const socket = FakeSocket.last();
    act(() => socket.serverOpen());
    await vi.waitFor(() => expect(socket.ops("options")).toHaveLength(1));
    return socket;
  }

  it("subscribes to the open expirations, replaces the rows with the engine's snapshot and moves each cell by itself", async () => {
    const socket = await opened();
    const ask = socket.ops("options")[0]!;
    expect(ask).toMatchObject({ symbol: "QQQ", expirations: ["2026-12-18"], strikes: 14 });
    const group = { ...GROUP, rows: [{ strike: 500, call: cell("C500", { bid: 3.1, live: true }), put: cell("P500") }] };
    act(() => socket.serverSend({ type: "options_snapshot", sub: ask.sub, seq: 1, epoch: "e1", underlying_price: 500.4, expirations: [group] }));
    const row = await screen.findByText("3.10", { exact: false });
    expect(row).toBeInTheDocument();
    act(() => socket.serverSend({ type: "options", sub: ask.sub, seq: 2, epoch: "e1", underlying_price: 500.5, changes: [{ symbol: "C500", bid: 3.4, live: true }] }));
    expect(await screen.findByText("3.40", { exact: false })).toBeInTheDocument();
    const rows = within(screen.getByTestId("options-view")).getAllByRole("row");
    expect(rows.length).toBeGreaterThan(2);
  });

  it("greys the values and says when the last update was while the source is down, and says the limit of symbols", async () => {
    const socket = await opened();
    const ask = socket.ops("options")[0]!;
    act(() => socket.serverSend({ type: "options_snapshot", sub: ask.sub, seq: 1, epoch: "e1", underlying_price: 500.4, expirations: [GROUP] }));
    act(() => socket.serverSend({ type: "options_status", sub: ask.sub, seq: 2, epoch: "e1", state: "reconnecting" }));
    expect(await screen.findByText(/Stopped\. Last update/)).toBeInTheDocument();
    expect(document.querySelector('[data-stale="true"]')).not.toBeNull();
    act(() => socket.serverSend({ type: "options_status", sub: ask.sub, seq: 3, epoch: "e1", state: "live" }));
    await vi.waitFor(() => expect(document.querySelector('[data-stale="true"]')).toBeNull());
    act(() => socket.serverSend({ type: "options_limit", sub: ask.sub, seq: 4, epoch: "e1", symbols: ["C501"], open: 22 }));
    expect(await screen.findByText(/symbol limit reached: 22 contracts open/)).toBeInTheDocument();
  });

  it("says the market state is unknown when the provider gave none, and says nothing when it is open", async () => {
    MARKET_OPEN = null;
    await opened();
    expect(await screen.findByText("Market state: unknown")).toBeInTheDocument();
  });

  it("with a source that has no streaming options the chain stays the photo", async () => {
    const socket = await opened();
    const ask = socket.ops("options")[0]!;
    act(() => socket.serverSend({ type: "error", sub: ask.sub, seq: 1, epoch: "e1", code: "market.options_not_live" }));
    expect(screen.getByTestId("options-live")).toBeEmptyDOMElement();
    expect(document.querySelector('[data-stale="true"]')).toBeNull();
  });
});
