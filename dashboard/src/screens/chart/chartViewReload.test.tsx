import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { t } from "@/i18n";
import { setHub } from "@/screens/chat/hub";
import { FakeSocket, bars, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { TEST_TOKEN } from "@/test/utils";
import { VIEW_DELAY_MS, flushPending, readLast, readTab } from "./chartState";
import { charts, useFakeCharts } from "./testChartLib";
import { mountHost, seedTabs } from "./testHost";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const NQ = "chart:symbol=NQ=F";
const T0 = 1793802600;
const STEP = 300;
const sleep = (ms: number) => act(() => new Promise<void>((done) => setTimeout(done, ms)));

beforeEach(() => {
  useFakeCharts();
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(async () => json({ items: [], suggested: true })));
  seedTabs([{ key: NQ, params: { symbol: "NQ=F" } }]);
});
afterEach(() => {
  flushPending();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

/** Opens the newest socket and answers its subscription with 60 bars. */
async function connect() {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, { bars: bars(60) })));
  await screen.findByTestId("chart-body");
  return { socket, sub };
}
const chart = () => charts.at(-1)!;
const body = () => screen.getByTestId("chart-body");

/** The student zooms and scrolls back 25 bars: the chart now shows bars 10 to 45 at a spacing of 11.5. */
function zoomAndScrollBack() {
  chart().view.range = { from: 9.6, to: 45.4 };
  chart().view.spacing = 11.5;
  chart().view.offset = -14;
  fireEvent.wheel(body());
}

describe("the view survives a reload", () => {
  it("R-01: zoom and scroll, wait 300 ms, reload: the same bars and the same spacing come back", async () => {
    const first = mountHost();
    await connect();
    zoomAndScrollBack();
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)?.view).toEqual({ follow: false, barSpacing: 11.5, rightOffset: -14, from: T0 + 10 * STEP, to: T0 + 45 * STEP, price: { auto: true } });
    expect(readLast()?.view).toEqual(readTab(NQ)?.view);
    first.unmount();

    FakeSocket.reset();
    useFakeCharts();
    mountHost();
    await connect();
    expect(chart().fitContent).not.toHaveBeenCalled();
    expect(chart().view.spacing).toBe(11.5);
    expect(chart().view.offset).toBe(45 - 59);
    const live = JSON.parse(body().getAttribute("data-view")!);
    expect(live).toMatchObject({ barSpacing: 11.5, follow: false });
  });

  it("R-11: a reload before 300 ms keeps the last zoom (pagehide writes it)", async () => {
    const first = mountHost();
    await connect();
    zoomAndScrollBack();
    expect(readTab(NQ)?.view).toBeNull(); // still waiting
    window.dispatchEvent(new Event("pagehide"));
    expect(readTab(NQ)?.view).toMatchObject({ barSpacing: 11.5, from: T0 + 10 * STEP });
    first.unmount();
    FakeSocket.reset();
    useFakeCharts();
    mountHost();
    await connect();
    expect(chart().view.spacing).toBe(11.5);
  });

  it("closing the tab right after a movement writes it too", async () => {
    const first = mountHost();
    await connect();
    zoomAndScrollBack();
    first.unmount();
    expect(readTab(NQ)?.view).toMatchObject({ barSpacing: 11.5 });
  });

  it("R-02: at the right edge it follows: spacing and offset come back, and the bars that arrive enter by the right", async () => {
    const first = mountHost();
    await connect();
    chart().view.spacing = 9;
    chart().view.offset = 2;
    fireEvent.wheel(body());
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)?.view).toMatchObject({ follow: true, barSpacing: 9, rightOffset: 2 });
    first.unmount();
    FakeSocket.reset();
    useFakeCharts();
    mountHost();
    const { socket, sub } = await connect();
    expect(chart().scrollToPosition).toHaveBeenCalledWith(2, false);
    expect(chart().view.spacing).toBe(9);
    // two new bars: update, never setData, never a refit
    for (const [k, n] of [[1, 60], [2, 61]] as const) {
      act(() => socket.serverSend({ type: "bar", sub, seq: k + 1, epoch: "epoch-aaaa", source: "Coinbase", data_state: "live", bar: { t: T0 + n * STEP, o: 1, h: 2, l: 0, c: 1, v: 1 }, final: false, price: 1, ts_ms: k, change: 0, change_pct: 0 }));
    }
    expect(chart().series[0]!.setData).toHaveBeenCalledTimes(1);
    expect(chart().series[0]!.update).toHaveBeenCalledTimes(2);
    expect(chart().fitContent).not.toHaveBeenCalled();
    expect(chart().view.spacing).toBe(9);
  });

  it("R-03: a dragged price scale comes back as it was, and a double click on it makes it automatic again", async () => {
    const first = mountHost();
    await connect();
    chart().view.autoScale = false;
    chart().view.priceRange = { from: 90.5, to: 130.25 };
    fireEvent.pointerDown(body());
    fireEvent.pointerUp(body());
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)?.view?.price).toEqual({ auto: false, min: 90.5, max: 130.25 });
    first.unmount();

    FakeSocket.reset();
    useFakeCharts();
    const second = mountHost();
    await connect();
    expect(chart().setAutoScale).toHaveBeenCalledWith(false);
    expect(chart().view.priceRange).toEqual({ from: 90.5, to: 130.25 });
    chart().view.autoScale = true; // the double click on the scale: the library makes it automatic
    fireEvent.doubleClick(body());
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)?.view?.price).toEqual({ auto: true });
    second.unmount();

    FakeSocket.reset();
    useFakeCharts();
    mountHost();
    await connect();
    expect(chart().setAutoScale).not.toHaveBeenCalled();
    expect(chart().view.autoScale).toBe(true);
  });

  it("R-07: a reconnection brings a new snapshot and the view does not change", async () => {
    mountHost();
    const { socket } = await connect();
    zoomAndScrollBack();
    const spacing = chart().view.spacing;
    const offset = chart().view.offset;
    act(() => socket.serverClose());
    // the engine comes back: the page opens a new socket and gets a fresh snapshot of the same chart
    await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(1), { timeout: 5000 });
    const again = FakeSocket.last();
    act(() => again.serverOpen());
    const sub = (again.ops("subscribe").at(-1) as { sub: string }).sub;
    // the engine rebuilt its window: five bars further on, so this one is a real snapshot, not a live change
    act(() => again.serverSend(snapshot(sub, 1, { bars: bars(60, T0 + 5 * STEP) })));
    await waitFor(() => expect(chart().series[0]!.setData).toHaveBeenCalledTimes(2));
    expect(charts).toHaveLength(1); // the same chart instance, not a new one
    expect(chart().fitContent).toHaveBeenCalledTimes(1); // only the first snapshot fitted
    expect(chart().view.spacing).toBe(spacing);
    // The same moment stays at the right edge: it is 5 bars nearer the start of the new window.
    expect(offset).toBe(-14);
    expect(chart().view.offset).toBe(40 - 59);
    expect(chart().setVisibleLogicalRange).not.toHaveBeenCalled();
  }, 15000);
});

describe("what resets the view", () => {
  it("a new interval goes back to the default view, in the store too", async () => {
    mountHost();
    await connect();
    zoomAndScrollBack();
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)?.view).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "7m{Enter}");
    expect(readTab(NQ)).toMatchObject({ tf: "7m", view: null });
    await connect();
    expect(chart().fitContent).toHaveBeenCalledTimes(1); // the new chart is fitted
    expect(chart().scrollToPosition).not.toHaveBeenCalled();
  });

  it("a movement waiting when the interval changes is dropped, not written under the new interval", async () => {
    mountHost();
    await connect();
    zoomAndScrollBack();
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "7m{Enter}");
    await sleep(VIEW_DELAY_MS + 60);
    expect(readTab(NQ)).toMatchObject({ tf: "7m", view: null });
  });

  it("a view saved for another symbol never reaches this one", async () => {
    localStorage.setItem(
      "hokma.chart.tabs.v1",
      JSON.stringify({ v: 1, tabs: { "chart:symbol=ES=F": { tf: "5m", view: { follow: false, barSpacing: 20, rightOffset: -5, from: T0, to: T0 + STEP, price: { auto: true } }, indicators: [], updated_at: 1 } }, last: null }),
    );
    mountHost();
    await connect();
    expect(chart().view.spacing).toBe(6);
    expect(chart().fitContent).toHaveBeenCalledTimes(1);
  });
});

describe("the live price is untouched by saving the view", () => {
  it("moving the view never subscribes or unsubscribes, and the header price follows the engine", async () => {
    mountHost();
    const { socket, sub } = await connect();
    const before = socket.sent.length;
    for (let i = 0; i < 30; i += 1) {
      chart().view.range = { from: i, to: i + 30 };
      chart().view.offset = -i;
      fireEvent.wheel(body());
    }
    await sleep(VIEW_DELAY_MS + 60);
    expect(socket.sent.length).toBe(before);
    expect(socket.ops("subscribe")).toHaveLength(1);
    expect(socket.ops("unsubscribe")).toHaveLength(0);
    act(() => socket.serverSend({ type: "last", sub, seq: 2, epoch: "epoch-aaaa", source: "Coinbase", data_state: "live", price: 321.5, ts_ms: 1, change: 1, change_pct: 1 }));
    expect(within(screen.getByTestId("chart-price").parentElement!).getByTestId("chart-price")).toHaveTextContent("321,50");
    expect(screen.getByTestId("chart-price")).toHaveAttribute("data-live", "true");
  });
});
