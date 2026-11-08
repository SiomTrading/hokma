import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { t } from "@/i18n";
import { getFocus } from "@/screens/connections/focus";
import { FakeServer } from "@/screens/chat/testkit";
import { setHub } from "@/screens/chat/hub";
import { useShell } from "@/shell/useShell";
import { FakeSocket, bars, envelope, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ChartScreen } from "./ChartScreen";
import { AssetSearch } from "./AssetSearch";
import { charts, fakeY, useFakeCharts } from "./testChartLib";

/** The candle series of the chart drawn last. */
const series = () => charts.at(-1)!.series[0]!;
const barsDrawn = () => Number(screen.getByTestId("chart-body").getAttribute("data-bars"));

/** jsdom has no canvas: a recording stand-in lets the real PNG export run end to end. */
const drawn: string[] = [];
function stubCanvas() {
  drawn.length = 0;
  const ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "left", textBaseline: "alphabetic",
    fillRect: () => undefined, beginPath: () => undefined, moveTo: () => undefined, lineTo: () => undefined,
    stroke: () => undefined, setLineDash: () => undefined, fill: () => undefined, rect: () => undefined, roundRect: () => undefined,
    drawImage: () => undefined, measureText: (text: string) => ({ width: text.length * 8 }),
    fillText: (text: string) => { drawn.push(text); },
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((done: BlobCallback) => done(new Blob(["png"], { type: "image/png" })));
}

const NINE_THIRTY = 1793802600;
const OPENING = { high: 105, session_date: "2026-11-04", start: NINE_THIRTY, end: NINE_THIRTY + 1800, complete: true };

type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let requests: Array<{ url: URL; init?: RequestInit }> = [];
function serve(handler: Handler) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    requests.push({ url, init });
    return handler(url, init);
  }));
}

function Probe() {
  const shell = useShell();
  return (
    <>
      <output aria-label="tabs">{shell.tabs.map((tab) => `${tab.screen}:${JSON.stringify(tab.params)}`).join("|")}</output>
      <output aria-label="toasts">{shell.toasts.map((toast) => toast.text).join("|")}</output>
    </>
  );
}

function show(params: Record<string, string> = { symbol: "NQ=F" }) {
  return renderWithApp(
    <>
      <ChartScreen tabKey="chart" params={params} active />
      <Probe />
    </>,
  );
}

/** Opens the socket of the chart that was just rendered and answers its first subscription with a snapshot. */
async function connect(fields: Record<string, unknown> = {}, extra: { source?: string; data_state?: string } = {}) {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, { opening_range: OPENING, ...fields }, extra)));
  return { socket, sub };
}

beforeEach(() => {
  useFakeCharts(); // jsdom has no canvas: the chart instance is a recording stand-in
  requests = [];
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
  serve(() => json({}));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

describe("live chart screen", () => {
  it("subscribes on /ws/market, draws the snapshot with the opening high line and says it is live", async () => {
    show();
    expect(screen.getByText(t("chart.loading"))).toBeInTheDocument();
    const { socket } = await connect();
    expect(new URL(socket.url).searchParams.get("token")).toBe(TEST_TOKEN);
    expect(socket.ops("subscribe")[0]).toMatchObject({ symbol: "NQ=F", tf: "5m" });
    await screen.findByRole("img", { name: t("chart.aria", { symbol: "NQ=F", tf: "5m" }) });
    expect(barsDrawn()).toBe(20);
    expect(series().setData).toHaveBeenCalledTimes(1);
    expect(series().setData.mock.calls[0]![0]).toHaveLength(20);
    expect(charts.at(-1)!.fitContent).toHaveBeenCalledTimes(1);
    expect(series().lines).toEqual([expect.objectContaining({ price: 105, axisLabelVisible: false, title: "" })]);
    expect(screen.getByTestId("opening-pill")).toHaveAttribute("data-price", "105");
    expect(screen.getByText(t("chart.opening"))).toBeInTheDocument();
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
    expect(screen.getByTestId("chart-change")).toHaveTextContent("+1,00%");
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "live");
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.live.live"));
    expect(screen.getByTestId("chart-price")).toHaveAttribute("data-live", "true");
    expect(screen.queryByTestId("connect-link")).toBeNull();
    expect(screen.getByText(t("chart.source", { source: "Coinbase" }))).toBeInTheDocument();
  });

  it("never asks the HTTP API for candles: the only requests are on the socket", async () => {
    show();
    const { socket, sub } = await connect();
    act(() => socket.serverSend(envelope("last", sub, 2, { price: 102, ts_ms: 1, change: 2, change_pct: 2 })));
    expect(requests.filter((r) => r.url.pathname.startsWith("/api/market/"))).toEqual([]);
  });

  it("shows a new price the moment the engine sends it", async () => {
    show();
    const { socket, sub } = await connect();
    const before = screen.getByTestId("chart-price").textContent;
    act(() => socket.serverSend(envelope("last", sub, 2, { price: 123.45, ts_ms: 1, change: 23.45, change_pct: 23.45 })));
    expect(screen.getByTestId("chart-price").textContent).not.toBe(before);
    expect(screen.getByTestId("chart-price")).toHaveTextContent("123,45");
    act(() => socket.serverSend(envelope("bar", sub, 3, { bar: { t: NINE_THIRTY + 20 * 300, o: 123, h: 125, l: 122, c: 124, v: 5 }, final: false, price: 124, ts_ms: 2, change: 24, change_pct: 24 })));
    expect(barsDrawn()).toBe(21);
    expect(series().update).toHaveBeenLastCalledWith(expect.objectContaining({ time: NINE_THIRTY + 20 * 300, open: 123, high: 125, low: 122, close: 124 }));
  });

  it("changing the timeframe subscribes again with it and drops the old candles", async () => {
    show();
    await connect();
    await userEvent.click(screen.getByRole("button", { name: "1D" }));
    expect(screen.queryByTestId("opening-pill")).toBeNull(); // the 5m candles never show under the 1D title
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 1D");
    expect(localStorage.getItem("hokma.chart.timeframe")).toBe("1D");
    const { socket } = await connect({ bars: bars(8), opening_range: null, tf: "1D" });
    expect(socket.ops("subscribe")[0]).toMatchObject({ tf: "1D" });
    await waitFor(() => expect(barsDrawn()).toBe(8));
    expect(series().setData.mock.calls.at(-1)![0]).toHaveLength(8);
  });

  it("says the market is closed and when the data was last seen", async () => {
    show();
    const { socket, sub } = await connect({ market_open: false });
    act(() => socket.serverSend(envelope("status", sub, 2, { state: "closed", code: "market.closed" })));
    const pill = screen.getByTestId("market-state");
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "closed");
    expect(pill).toHaveTextContent(t("chart.market.closed"));
    expect(pill.textContent).toMatch(/\d\d:\d\d/);
  });

  it("says Reconnecting when the socket drops, keeps the candles and goes back to Live with the new snapshot", async () => {
    show();
    const { socket } = await connect();
    act(() => socket.serverClose(1011));
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "reconnecting");
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.live.reconnecting"));
    expect(barsDrawn()).toBe(20);
    const next = await waitFor(() => {
      const last = FakeSocket.last();
      if (last === socket) throw new Error("not yet");
      return last;
    }, { timeout: 3000 });
    act(() => next.serverOpen());
    const sub = (next.ops("subscribe").at(-1) as { sub: string }).sub;
    act(() => next.serverSend(snapshot(sub, 1)));
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "live");
  });

  it("an empty snapshot is an empty state, not a blank chart", async () => {
    show();
    await connect({ bars: [], last: null, opening_range: null });
    expect(await screen.findByText(t("chart.empty.title"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("chart.ask") })).toBeDisabled();
  });
});

describe("price chart", () => {
  it("the live candle goes through update(); setData runs only for the snapshot (T-13, 4 messages a second)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      show();
      const { socket, sub } = await connect();
      expect(series().setData).toHaveBeenCalledTimes(1);
      let seq = 2;
      // three seconds of a live bar at 4 messages a second: the last bar moves, then a new one opens
      for (let tick = 0; tick < 12; tick += 1) {
        const opensNew = tick >= 8;
        const t0 = NINE_THIRTY + (opensNew ? 20 : 19) * 300;
        const price = 100 + tick / 4;
        act(() => socket.serverSend(envelope("bar", sub, seq++, { bar: { t: t0, o: 100, h: price + 1, l: 99, c: price, v: 10 + tick }, final: false, price, ts_ms: tick, change: 0, change_pct: 0 })));
        act(() => vi.advanceTimersByTime(250));
      }
      expect(series().setData).toHaveBeenCalledTimes(1);
      expect(series().update).toHaveBeenCalledTimes(12);
      expect(charts.at(-1)!.fitContent).toHaveBeenCalledTimes(1);
      expect(barsDrawn()).toBe(21);
      expect(series().data).toHaveLength(21);
      expect(series().data.at(-1)).toMatchObject({ time: NINE_THIRTY + 20 * 300, close: 102.75 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("a new snapshot on the same chart (a reconnection) replaces the bars and does not fit them again", async () => {
    show();
    const { socket, sub } = await connect();
    act(() => socket.serverSend(snapshot(sub, 2, { opening_range: OPENING, bars: bars(12, NINE_THIRTY + 3600) })));
    expect(series().setData).toHaveBeenCalledTimes(2);
    expect(series().setData.mock.calls[1]![0]).toHaveLength(12);
    expect(charts.at(-1)!.fitContent).toHaveBeenCalledTimes(1); // the view is the student's; R-07 covers it in detail
  });

  it("the readout shows the bar under the pointer and goes away with it", async () => {
    show();
    await connect();
    expect(screen.queryByTestId("chart-readout")).toBeNull();
    const first = bars(20)[3]!;
    act(() => charts.at(-1)!.hover(first.t));
    const readout = screen.getByTestId("chart-readout");
    expect(readout).toHaveTextContent(new Intl.NumberFormat("es", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(first.h));
    expect(readout).toHaveTextContent("14:45"); // the fake snapshot is in UTC
    act(() => charts.at(-1)!.hover(undefined));
    expect(screen.queryByTestId("chart-readout")).toBeNull();
  });

  it("the opening pill sits at the y of its line", async () => {
    show();
    await connect();
    const pill = screen.getByTestId("opening-pill");
    expect(pill.style.top).toBe(`${fakeY(105) - 26}px`);
    expect(pill.style.right).toBe("92px"); // price axis width + 12
  });

  it("the chart looks like the design: transparent, horizontal grid only, 78 px price axis in mono, vertical crosshair only", async () => {
    show();
    await connect();
    const options = charts.at(-1)!.options as Record<string, Record<string, unknown>>;
    expect(options.layout).toMatchObject({ background: { type: "solid", color: "transparent" }, fontSize: 10 });
    expect(String(options.layout!.fontFamily)).toContain("JetBrains Mono");
    expect(options.layout).not.toHaveProperty("attributionLogo"); // the library's attribution stays on, as it ships
    expect(options.grid).toMatchObject({ vertLines: { visible: false } });
    expect(options.rightPriceScale).toMatchObject({ minimumWidth: 78 });
    expect(options.crosshair).toMatchObject({ horzLine: { visible: false, labelVisible: false }, vertLine: { labelVisible: false } });
    expect(series().options).toMatchObject({ priceLineVisible: false, lastValueVisible: false });
    const body = screen.getByTestId("chart-body");
    expect(body).toHaveAttribute("tabindex", "0");
    expect(body.style.minHeight).toBe("260px");
  });

  it("the time axis and the readout show the time of the symbol's zone", async () => {
    show();
    await connect();
    const options = charts.at(-1)!.options as { timeScale: { tickMarkFormatter: (time: number) => string } };
    expect(options.timeScale.tickMarkFormatter(NINE_THIRTY)).toBe("14:30"); // the fake snapshot is in UTC
  });

  it("the chart is removed when the screen goes away", async () => {
    const view = show();
    await connect();
    const chart = charts.at(-1)!;
    view.unmount();
    expect(chart.remove).toHaveBeenCalledTimes(1);
  });
});

describe("live price over a delayed history (task 3081)", () => {
  const stock = { asset_class: "equity", name: "Invesco QQQ", kind: "equity", exchange: "NASDAQ" };

  it("a live Finnhub stock says Live · Finnhub and says the history is Yahoo's, delayed", async () => {
    show({ symbol: "QQQ" });
    await connect({ ...stock, delay_seconds: 0, history: { source: "Yahoo Finance", delay_seconds: 900 } }, { source: "Finnhub", data_state: "live" });
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "live");
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.route.live", { source: "Finnhub" }));
    expect(screen.getByTestId("market-state")).not.toHaveTextContent(t("chart.live.delayed"));
    expect(screen.getByTestId("history-notice")).toHaveTextContent(t("chart.live.history", { source: "Yahoo Finance", minutes: 15 }));
    expect(screen.queryByTestId("connect-link")).toBeNull();
  });

  it("shows no history notice when the history is not late (crypto)", async () => {
    show();
    await connect({ history: { source: "Yahoo Finance", delay_seconds: 0 } });
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.route.live", { source: "Coinbase" }));
    expect(screen.queryByTestId("history-notice")).toBeNull();
  });
});

describe("delayed data (C6)", () => {
  const delayed = { fallback: true, delay_seconds: 900, asset_class: "equity", name: "Apple", kind: "equity" };
  const yahoo = { source: "Yahoo Finance", data_state: "delayed" };

  it("without a provider the chart says Delayed with the minutes, is never painted as live, and links to Connections", async () => {
    show({ symbol: "AAPL" });
    await connect(delayed, yahoo);
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "delayed");
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-data-state", "delayed");
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.live.delayedMinutes", { minutes: 15 }));
    expect(screen.getByTestId("market-state")).not.toHaveTextContent(t("chart.live.live"));
    expect(screen.getByTestId("chart-price")).toHaveAttribute("data-live", "false");
    const link = screen.getByTestId("connect-link");
    expect(link).toHaveTextContent(t("chart.live.connect"));
    expect(link).toHaveAttribute("href", "#/connections?class=equity");
  });

  it("a status that says live does not turn delayed data into live", async () => {
    show({ symbol: "AAPL" });
    const { socket, sub } = await connect(delayed, yahoo);
    act(() => socket.serverSend(envelope("status", sub, 2, { state: "live", code: "ok" }, yahoo)));
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "delayed");
  });

  it("the link opens Connections and asks for the group of that class", async () => {
    show({ symbol: "AAPL" });
    await connect(delayed, yahoo);
    await userEvent.click(screen.getByTestId("connect-link"));
    expect(screen.getByLabelText("tabs")).toHaveTextContent("connections:{}");
    expect(getFocus().assetClass).toBe("equity");
  });

  it("with the market closed it shows both: closed, and delayed", async () => {
    show({ symbol: "AAPL" });
    const { socket, sub } = await connect({ ...delayed, market_open: false }, yahoo);
    act(() => socket.serverSend(envelope("status", sub, 2, { state: "closed", code: "market.closed" }, yahoo)));
    expect(screen.getByTestId("live-status")).toHaveAttribute("data-mode", "closed");
    expect(screen.getByTestId("delay-notice")).toHaveTextContent(t("chart.live.delayedMinutes", { minutes: 15 }));
    expect(screen.getByTestId("connect-link")).toBeInTheDocument();
  });
});

describe("errors of the live channel (C7 of the chart)", () => {
  async function fail(code: string, message = "server text") {
    const { socket, sub } = await connect();
    act(() => socket.serverSend({ type: "error", sub, code, message, epoch: "epoch-aaaa", source: "none", data_state: "delayed" }));
  }

  it("market.limit is visible in the chart, with the link to Connections, and does not break another chart", async () => {
    renderWithApp(
      <>
        <ChartScreen tabKey="a" params={{ symbol: "AAPL" }} active />
        <ChartScreen tabKey="b" params={{ symbol: "MSFT" }} active />
        <Probe />
      </>,
    );
    const sockets = await waitFor(() => {
      const list = FakeSocket.of("/ws/market");
      if (list.length < 2) throw new Error("not yet");
      return list;
    });
    for (const [index, socket] of sockets.entries()) {
      act(() => socket.serverOpen());
      const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
      if (index === 0) act(() => socket.serverSend({ type: "error", sub, code: "market.limit", message: "x", epoch: "epoch-aaaa", source: "none", data_state: "delayed" }));
      else act(() => socket.serverSend(snapshot(sub, 1)));
    }
    const alert = await screen.findByTestId("live-error");
    expect(alert).toHaveAttribute("data-code", "market.limit");
    expect(alert).toHaveTextContent(t("market.err.limit"));
    expect(within(alert).getByTestId("connect-link")).toHaveAttribute("href", "#/connections");
    expect(barsDrawn()).toBe(20); // the other chart still draws
  });

  it("market.not_entitled says so and links to Connections", async () => {
    show();
    await fail("market.not_entitled");
    const alert = await screen.findByTestId("live-error");
    expect(alert).toHaveTextContent(t("market.err.not_entitled"));
    expect(within(alert).getByTestId("connect-link")).toBeInTheDocument();
  });

  it("an unknown symbol is an error with a retry that subscribes again, not an empty chart", async () => {
    show({ symbol: "ZZZZ" });
    const { socket } = await connect().then(async (c) => {
      act(() => c.socket.serverSend({ type: "error", sub: c.sub, code: "market.unknown_symbol", message: "x", epoch: "epoch-aaaa", source: "none", data_state: "delayed" }));
      return c;
    });
    const alert = await screen.findByTestId("live-error");
    expect(alert).toHaveTextContent(t("market.err.unknown_symbol"));
    expect(within(alert).queryByTestId("connect-link")).toBeNull();
    expect(screen.getByRole("button", { name: t("chart.ask") })).toBeDisabled();
    await userEvent.click(within(alert).getByRole("button", { name: t("ui.error.retry") }));
    expect(socket.ops("subscribe")).toHaveLength(2);
    expect(socket.ops("subscribe")[1]).toMatchObject({ symbol: "ZZZZ" });
  });
});

describe("without a symbol", () => {
  it("asks for one and opens the chart after the pick", async () => {
    serve((url) => {
      if (url.pathname === "/api/market/symbols") return json({ items: [{ symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" }], suggested: true });
      return json({});
    });
    show({});
    expect(screen.getByText(t("chart.none.title"))).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("menuitem", { name: /ES=F/ }));
    expect(await screen.findByTestId("chart-title")).toHaveTextContent("ES=F · 5m");
    expect(FakeSocket.last().ops("subscribe")).toEqual([]); // not open yet
  });
});

describe("ask the fund about this chart", () => {
  beforeEach(stubCanvas);

  it("uploads the image, sends it in a new conversation and opens the chat tab", async () => {
    const server = new FakeServer();
    serve((url) => (url.pathname === "/api/attachments" ? json({ id: "att-1", name: "chart-NQ-F-5m.png", mime: "image/png", size: 3, kind: "image" }, 201) : json({})));
    show();
    await connect();
    await screen.findByTestId("opening-pill");

    await userEvent.click(screen.getByRole("button", { name: t("chart.ask") }));
    await waitFor(() => expect(server.handlers.onOpen).toBeDefined());
    server.open();
    await waitFor(() => expect(server.last("send")).toBeDefined());

    expect(drawn).toContain("NQ=F · 5m"); // the image is drawn from the chart on screen
    const upload = requests.find((r) => r.url.pathname === "/api/attachments")!;
    expect(upload.init?.method).toBe("POST");
    const form = upload.init!.body as FormData;
    const file = form.get("file") as File;
    expect(file.name).toBe("chart-NQ-F-5m.png");
    expect(file.type).toBe("image/png");

    const sent = server.last("send")!;
    expect(sent).toMatchObject({ type: "send", conversation_id: null, attachments: ["att-1"] });
    expect(sent.text).toContain("NQ=F");
    expect(sent.text).toContain("5m");
    expect(sent.text).toContain("105"); // the opening high travels in the question

    server.emit({ type: "conversation", conversation_id: "42", title: "x", model: "sonnet", agent: null, project: null, created: true });
    await waitFor(() => expect(screen.getByLabelText("tabs")).toHaveTextContent('chat:{"conversation":"42"}'));
  });

  it("tells the owner when the chat cannot be reached", async () => {
    const server = new FakeServer();
    serve((url) => (url.pathname === "/api/attachments" ? json({ id: "att-1", name: "a.png", mime: "image/png", size: 3, kind: "image" }, 201) : json({})));
    show();
    await connect();
    await screen.findByTestId("opening-pill");
    await userEvent.click(screen.getByRole("button", { name: t("chart.ask") }));
    await waitFor(() => expect(server.handlers.onOpen).toBeDefined());
    server.open();
    await waitFor(() => expect(server.last("send")).toBeDefined());
    server.emit({ type: "error", code: "claude_not_found", message: "no claude" });
    await waitFor(() => expect(screen.getByLabelText("toasts")).toHaveTextContent(t("chart.ask.failed")));
    expect(screen.getByLabelText("tabs")).not.toHaveTextContent("conversation");
    expect(screen.getByRole("button", { name: t("chart.ask") })).toBeEnabled();
  });

  it("tells the owner when the upload fails", async () => {
    new FakeServer();
    serve((url) => (url.pathname === "/api/attachments" ? json({ detail: { code: "too_big", message: "too big" } }, 413) : json({})));
    show();
    await connect();
    await screen.findByTestId("opening-pill");
    await userEvent.click(screen.getByRole("button", { name: t("chart.ask") }));
    await waitFor(() => expect(screen.getByLabelText("toasts")).toHaveTextContent(t("chart.ask.failed")));
  });
});

describe("asset search", () => {
  it("lists suggestions first, searches while typing and opens the picked symbol", async () => {
    serve((url) => {
      const q = url.searchParams.get("q");
      if (q === "") return json({ items: [{ symbol: "NQ=F", name: "E-mini Nasdaq 100", kind: "future", exchange: "CME" }], suggested: true });
      if (q === "aapl") return json({ items: [{ symbol: "AAPL", name: "Apple Inc.", kind: "equity", exchange: "NASDAQ" }], suggested: false });
      return json({ items: [], suggested: false });
    });
    const picked: string[] = [];
    renderWithApp(<AssetSearch onPick={(s) => picked.push(s)} />);
    expect(await screen.findByRole("menuitem", { name: /NQ=F/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: t("chart.search.suggested") })).toBeInTheDocument();

    await userEvent.type(screen.getByRole("textbox", { name: t("chart.search.label") }), "aapl");
    const item = await screen.findByRole("menuitem", { name: /AAPL/ });
    expect(screen.queryByRole("menuitem", { name: /NQ=F/ })).toBeNull();
    await userEvent.click(item);
    expect(picked).toEqual(["AAPL"]);
  });

  it("says when nothing matches and when the search fails, with retry", async () => {
    let fail = true;
    serve((url) => {
      if (url.searchParams.get("q") === "zzz") return fail ? json({ detail: { code: "network", message: "Search is down" } }, 502) : json({ items: [], suggested: false });
      return json({ items: [], suggested: true });
    });
    renderWithApp(<AssetSearch onPick={() => undefined} />);
    await userEvent.type(screen.getByRole("textbox", { name: t("chart.search.label") }), "zzz");
    expect(await screen.findByRole("alert")).toHaveTextContent("Search is down");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByText(t("chart.search.empty", { query: "zzz" }))).toBeInTheDocument();
  });

  it("Enter opens the typed ticker when nothing better is known yet", async () => {
    serve(() => json({ items: [], suggested: true }));
    const picked: string[] = [];
    renderWithApp(<AssetSearch onPick={(s) => picked.push(s)} />);
    const box = screen.getByRole("textbox", { name: t("chart.search.label") });
    await userEvent.type(box, "nq=f{Enter}");
    expect(picked).toEqual(["NQ=F"]);
  });
});
