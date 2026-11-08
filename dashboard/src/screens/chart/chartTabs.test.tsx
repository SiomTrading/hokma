import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { t } from "@/i18n";
import { setHub } from "@/screens/chat/hub";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN } from "@/test/utils";
import { TABS_KEY, flushPending, readLast, readTab, seedTab, tabKeys } from "./chartState";
import { useFakeCharts } from "./testChartLib";
import { mountHost, seedTabs, shellApi } from "./testHost";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const HITS = [
  { symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" },
  { symbol: "NQ=F", name: "E-mini Nasdaq", kind: "future", exchange: "CME" },
];

const NQ = "chart:symbol=NQ=F";
const ES = "chart:symbol=ES=F";
const BTC = "chart:symbol=BTC-USD";

beforeEach(() => {
  useFakeCharts();
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(async () => json({ items: HITS, suggested: true })));
});
afterEach(() => {
  flushPending();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

/** The engine side opens the newest socket, which is when the subscription goes out. */
const open = () => act(() => FakeSocket.last().serverOpen());
const subscribes = () => FakeSocket.of("/ws/market").flatMap((s) => s.ops("subscribe"));
const keys = () => screen.getByLabelText("tab-keys").textContent;
/** Inside the tab in front: the title shows its asset and interval. */
const front = () => within(document.querySelector<HTMLElement>("[data-tab]:not([hidden])")!);

async function pickInterval(text: string) {
  await userEvent.click(front().getByRole("button", { name: t("chart.interval.more") }));
  await userEvent.type(await screen.findByRole("combobox"), text + "{Enter}");
}

describe("a tab remembers its interval", () => {
  it("two tabs keep their own interval across a reload", async () => {
    seedTabs([
      { key: NQ, params: { symbol: "NQ=F" } },
      { key: BTC, params: { symbol: "BTC-USD" } },
    ], NQ);
    const first = mountHost();
    await pickInterval("7m");
    act(() => shellApi().activateTab(BTC));
    await pickInterval("1h");
    expect(readTab(NQ)?.tf).toBe("7m");
    expect(readTab(BTC)?.tf).toBe("1h");
    first.unmount();

    FakeSocket.reset();
    mountHost();
    const titles = Array.from(document.querySelectorAll("[data-testid=chart-title]")).map((node) => node.textContent);
    expect(titles).toEqual(["NQ=F · 7m", "BTC-USD · 1h"]);
    expect(readTab(NQ)?.tf).toBe("7m");
    expect(readTab(BTC)?.tf).toBe("1h");
  });

  it("the interval is saved the moment it changes, and the entry carries an empty indicators list", async () => {
    seedTabs([{ key: NQ, params: { symbol: "NQ=F" } }]);
    mountHost();
    await pickInterval("3h");
    const saved = JSON.parse(localStorage.getItem(TABS_KEY)!);
    expect(saved.v).toBe(1);
    expect(saved.tabs[NQ]).toMatchObject({ tf: "3h", view: null, indicators: [] });
    expect(saved.last).toMatchObject({ symbol: "NQ=F", tf: "3h" });
  });

  it("changing the asset: same interval, default view, no old key left, after a reload too", async () => {
    seedTabs([{ key: NQ, params: { symbol: "NQ=F" } }]);
    const first = mountHost();
    await pickInterval("7m");
    await userEvent.click(front().getByTestId("chart-symbol"));
    await userEvent.click(await screen.findByRole("option", { name: /ES=F/ }));
    expect(keys()).toBe(ES);
    expect(tabKeys()).toEqual([ES]);
    expect(readTab(ES)).toMatchObject({ tf: "7m", view: null });
    first.unmount();

    FakeSocket.reset();
    mountHost();
    expect(front().getByTestId("chart-title")).toHaveTextContent("ES=F · 7m");
    expect(tabKeys()).toEqual([ES]);
  });

  it("changing to an asset that already has a tab leaves one tab with its own state and no orphan", async () => {
    seedTabs([
      { key: NQ, params: { symbol: "NQ=F" } },
      { key: ES, params: { symbol: "ES=F" } },
    ], ES);
    seedTab(NQ, "7m", null);
    seedTab(ES, "1h", null);
    mountHost();
    await userEvent.click(front().getByTestId("chart-symbol"));
    await userEvent.click(await screen.findByRole("option", { name: /NQ=F/ }));
    expect(keys()).toBe(NQ);
    expect(tabKeys()).toEqual([NQ]);
    expect(readTab(NQ)?.tf).toBe("7m");
    expect(front().getByTestId("chart-title")).toHaveTextContent("NQ=F · 7m");
  });
});

describe("reading what is saved", () => {
  it("broken JSON gives the default interval, no error, and the open tabs stay open", async () => {
    seedTabs([
      { key: NQ, params: { symbol: "NQ=F" } },
      { key: ES, params: { symbol: "ES=F" } },
    ], NQ);
    localStorage.setItem(TABS_KEY, "{broken");
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mountHost();
    expect(front().getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
    expect(keys()).toBe(NQ + "|" + ES);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(errors).not.toHaveBeenCalled();
  });

  it("an interval the grammar rejects falls back to the default one", async () => {
    seedTabs([{ key: NQ, params: { symbol: "NQ=F" } }]);
    localStorage.setItem(TABS_KEY, JSON.stringify({ v: 1, tabs: { [NQ]: { tf: "99M", view: null, indicators: [], updated_at: 1 } }, last: null }));
    mountHost();
    expect(front().getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
    await waitFor(() => FakeSocket.last());
    open();
    expect(subscribes().at(-1)).toMatchObject({ tf: "5m" });
  });

  it("on load, the tabs that are not open lose their entry", async () => {
    seedTabs([{ key: NQ, params: { symbol: "NQ=F" } }]);
    seedTab(NQ, "7m", null);
    seedTab(ES, "1h", null);
    mountHost();
    expect(tabKeys()).toEqual([NQ]);
    expect(readLast()).not.toBeNull();
  });
});

describe("a new chart opens with the last one the student saw", () => {
  async function seeES() {
    seedTabs([{ key: ES, params: { symbol: "ES=F" } }]);
    mountHost();
    await pickInterval("15m");
  }

  it("closing the chart and opening a new one brings back its asset and interval", async () => {
    await seeES();
    act(() => shellApi().closeTab(ES));
    expect(keys()).toBe("");
    act(() => shellApi().openScreen("chart"));
    expect(keys()).toBe(ES);
    expect(front().getByTestId("chart-title")).toHaveTextContent("ES=F · 15m");
    await waitFor(() => FakeSocket.last());
    open();
    expect(subscribes().at(-1)).toMatchObject({ symbol: "ES=F", tf: "15m" });
  });

  it("with that asset still open, the new tab is empty with the search and nothing merges", async () => {
    await seeES();
    act(() => shellApi().openScreen("chart"));
    expect(keys()).toBe(ES + "|chart");
    expect(front().getByText(t("chart.none.title"))).toBeInTheDocument();
    expect(front().getByPlaceholderText(t("chart.search.placeholder"))).toBeInTheDocument();
  });

  it("an asset opened from another screen takes the last interval and the default view", async () => {
    await seeES();
    act(() => shellApi().openScreen("chart", { symbol: "BTC-USD" }));
    expect(keys()).toBe(ES + "|" + BTC);
    expect(front().getByTestId("chart-title")).toHaveTextContent("BTC-USD · 15m");
    expect(readTab(BTC)).toMatchObject({ tf: "15m", view: null });
  });

  it("without a last chart a new tab is the empty screen", () => {
    seedTabs([]);
    mountHost();
    act(() => shellApi().openScreen("chart"));
    expect(front().getByText(t("chart.none.title"))).toBeInTheDocument();
  });

  it("the tab put in front becomes the last chart", async () => {
    seedTabs([
      { key: NQ, params: { symbol: "NQ=F" } },
      { key: ES, params: { symbol: "ES=F" } },
    ], NQ);
    mountHost();
    expect(readLast()).toMatchObject({ symbol: "NQ=F" });
    act(() => shellApi().activateTab(ES));
    expect(readLast()).toMatchObject({ symbol: "ES=F" });
  });
});

describe("sixty tabs opened and closed", () => {
  it("the store never holds more than 50 tabs and keeps last", async () => {
    seedTabs([]);
    mountHost();
    for (let i = 0; i < 60; i += 1) {
      const symbol = "SYM" + i;
      act(() => shellApi().openScreen("chart", { symbol }));
      act(() => shellApi().closeTab("chart:symbol=" + symbol));
      expect(tabKeys().length).toBeLessThanOrEqual(50);
    }
    expect(readLast()).not.toBeNull();
    expect(tabKeys().length).toBeLessThanOrEqual(50);
  });
});
