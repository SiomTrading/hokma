import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setHub } from "@/screens/chat/hub";
import { FakeSocket, envelope, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN } from "@/test/utils";
import { formatMoment } from "./format";
import { mountHost, seedTabs } from "./testHost";
import { useFakeCharts } from "./testChartLib";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

const FIRST = 1793802600;
const rangeBars = (count: number, approxUntil: number | null) =>
  Array.from({ length: count }, (_, i) => {
    const base = 28300 + i * 2.5;
    return { t: FIRST + i * 7, i: i + 40, o: base, h: base + 2.5, l: base, c: base + 2.5, v: 10 + i, approx: approxUntil !== null && FIRST + i * 7 < approxUntil };
  });
const RANGE = { kind: "range", size: 10, unit: "t", tick_size: 0.25 };

beforeEach(() => {
  setLocale("en");
  useFakeCharts();
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(async () => json({ items: [], suggested: true })));
  seedTabs([{ key: "chart:symbol=NQ=F", params: { symbol: "NQ=F" } }]);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

async function connect(fields: Record<string, unknown>, extra: { source?: string; data_state?: string } = {}) {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, { symbol: "NQ=F", kind: "future", asset_class: "future", ...fields }, extra)));
  return { socket, sub };
}

describe("range and seconds notices (T-7, T-8: the panel half)", () => {
  it("a range built only from 1-minute bars says so, and offers to connect a source", async () => {
    mountHost();
    await connect(
      { tf: "r:10t", fallback: true, interval: RANGE, tick_size: 0.25, exact_from: null, approx_until: null, bars: rangeBars(12, Number.MAX_SAFE_INTEGER) },
      { source: "Yahoo Finance", data_state: "delayed" },
    );
    const notice = await screen.findByTestId("chart-notice");
    expect(notice).toHaveAttribute("data-kind", "approx_all");
    expect(notice).toHaveTextContent(t("chart.notice.approxAll"));
    expect(screen.getByTestId("connect-link")).toBeInTheDocument();
  });

  it("a range with trades is approximate only before the first trade, with its time", async () => {
    mountHost();
    const until = FIRST + 5 * 7;
    await connect({ tf: "r:10t", interval: RANGE, tick_size: 0.25, exact_from: until, approx_until: until, bars: rangeBars(12, until) });
    const notice = await screen.findByTestId("chart-notice");
    expect(notice).toHaveAttribute("data-kind", "approx_before");
    expect(notice).toHaveTextContent(t("chart.notice.approxBefore", { time: formatMoment(until, "UTC", "en") }));
  });

  it("the notice goes away when the trades cover every bar on screen", async () => {
    mountHost();
    await connect({ tf: "r:10t", interval: RANGE, tick_size: 0.25, exact_from: FIRST - 100, approx_until: FIRST - 100, bars: rangeBars(12, null) });
    await screen.findByTestId("chart-title");
    expect(screen.queryByTestId("chart-notice")).toBeNull();
  });

  it("seconds bars say where they start", async () => {
    mountHost();
    await connect({ tf: "15s", interval: { kind: "time", seconds: 15 }, tick_size: 0.25, exact_from: FIRST, bars: rangeBars(6, null).map(({ i, approx, ...bar }) => bar) });
    const notice = await screen.findByTestId("chart-notice");
    expect(notice).toHaveAttribute("data-kind", "seconds_start");
    expect(notice).toHaveTextContent(t("chart.notice.secondsStart", { time: formatMoment(FIRST, "UTC", "en") }));
  });

  it("a price range rounded to the tick says to what", async () => {
    mountHost();
    await connect({ tf: "r:2.6p", interval: { kind: "range", size: 2.5, unit: "p", tick_size: 0.25, rounded: true }, tick_size: 0.25, exact_from: FIRST - 100, approx_until: null, bars: rangeBars(6, null) });
    expect(await screen.findByTestId("range-rounded")).toHaveTextContent(t("chart.notice.rounded", { size: "2.50" }));
  });

  it("seconds on a source without trades are an empty state with the link to connect one", async () => {
    mountHost();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
    act(() => socket.serverSend(envelope("error", sub, 1, { code: "market.seconds_need_ticks", message: "", params: {} }, { source: "none", data_state: "delayed" })));
    const alert = await screen.findByTestId("live-error");
    expect(alert).toHaveTextContent(t("market.err.seconds_need_ticks"));
    expect(screen.getByTestId("connect-link")).toBeInTheDocument();
  });

  it("an interval the source cannot build names the base, the source and the symbol", async () => {
    mountHost();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
    const params = { interval: "r:10t", base: "1m", source: "Yahoo Finance", symbol: "NQ=F" };
    act(() => socket.serverSend(envelope("error", sub, 1, { code: "market.interval_unavailable", message: "", params }, { source: "none", data_state: "delayed" })));
    expect(await screen.findByTestId("live-error")).toHaveTextContent(t("market.err.interval_unavailable", params));
  });
});

describe("the interval box knows what the source can do", () => {
  it("seconds are disabled on the delayed fallback", async () => {
    mountHost();
    await connect(
      { tf: "5m", fallback: true, interval: { kind: "time", seconds: 300 }, tick_size: 0.25 },
      { source: "Yahoo Finance", data_state: "delayed" },
    );
    await userEvent.click(await screen.findByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "15");
    const seconds = within(screen.getByRole("listbox")).getAllByRole("option")[0]!;
    expect(seconds).toHaveAttribute("aria-disabled", "true");
    expect(seconds).toHaveAttribute("title", t("market.err.seconds_need_ticks"));
  });

  it("seconds are available when the source gives trades", async () => {
    mountHost();
    await connect({ tf: "5m", fallback: false, interval: { kind: "time", seconds: 300 }, tick_size: 0.25 });
    await userEvent.click(await screen.findByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "15");
    const seconds = within(screen.getByRole("listbox")).getAllByRole("option")[0]!;
    expect(seconds).not.toHaveAttribute("aria-disabled", "true");
  });

  it("a range in ticks is not offered for an instrument without a known tick size", async () => {
    mountHost();
    await connect({ tf: "5m", symbol: "NQ=F", interval: { kind: "time", seconds: 300 }, tick_size: null });
    await userEvent.click(await screen.findByRole("button", { name: t("chart.interval.more") }));
    expect(await screen.findByText(t("chart.interval.ticksUnavailable"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("chart.interval.ticks") })).toBeNull();
  });

  it("and it is offered when the tick size is known", async () => {
    mountHost();
    await connect({ tf: "5m", interval: { kind: "time", seconds: 300 }, tick_size: 0.25 });
    await userEvent.click(await screen.findByRole("button", { name: t("chart.interval.more") }));
    expect(await screen.findByRole("button", { name: t("chart.interval.ticks") })).toBeInTheDocument();
    expect(screen.queryByText(t("chart.interval.ticksUnavailable"))).toBeNull();
  });
});
