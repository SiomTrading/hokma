import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { t } from "@/i18n";
import { FakeServer } from "@/screens/chat/testkit";
import { setHub } from "@/screens/chat/hub";
import { useShell } from "@/shell/useShell";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ChartScreen } from "./ChartScreen";
import { REFRESH_SECONDS } from "./useChartData";
import { AssetSearch } from "./AssetSearch";
import type { ChartData, Timeframe } from "./types";

/** jsdom has no canvas: a recording stand-in lets the real PNG export run end to end. */
const drawn: string[] = [];
function stubCanvas() {
  drawn.length = 0;
  const ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "left", textBaseline: "alphabetic",
    fillRect: () => undefined, beginPath: () => undefined, moveTo: () => undefined, lineTo: () => undefined,
    stroke: () => undefined, setLineDash: () => undefined,
    fillText: (text: string) => { drawn.push(text); },
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((done: BlobCallback) => done(new Blob(["png"], { type: "image/png" })));
}

const NINE_THIRTY = 1793802600;

function chart(overrides: Partial<ChartData> = {}): ChartData {
  const candles = Array.from({ length: 20 }, (_, i) => {
    const base = 100 + Math.sin(i) * 3;
    return { t: NINE_THIRTY + i * 300, o: base, h: base + 2, l: base - 2, c: base + (i % 2 ? -1 : 1), v: 1000 + i };
  });
  return {
    symbol: "NQ=F", name: "Nasdaq 100 Dec 26", kind: "future", exchange: "CME", currency: "USD", timezone: "America/New_York",
    timeframe: "5m", candles, last_price: 101, previous_close: 100, change: 1, change_pct: 1, market_open: true,
    last_trade_at: NINE_THIRTY + 19 * 300, data_age_seconds: 20, delayed: false,
    opening_range: { high: 105, session_date: "2026-11-04", start: NINE_THIRTY, end: NINE_THIRTY + 1800, complete: true },
    source: "Yahoo Finance", stale: false, fetched_at: NINE_THIRTY + 19 * 300, ...overrides,
  };
}

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

beforeEach(() => {
  requests = [];
  clearToken();
  setToken(TEST_TOKEN);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

describe("chart screen", () => {
  it("loads candles for the symbol and draws them with the opening high line and pill", async () => {
    serve(() => json(chart()));
    show();
    expect(screen.getByText(t("chart.loading"))).toBeInTheDocument(); // loading state first
    await screen.findByRole("img", { name: t("chart.aria", { symbol: "NQ=F", tf: "5m" }) });
    expect(requests[0]!.url.pathname).toBe("/api/market/candles");
    expect(requests[0]!.url.searchParams.get("symbol")).toBe("NQ=F");
    expect(requests[0]!.url.searchParams.get("tf")).toBe("5m");
    expect(requests[0]!.init?.headers).toMatchObject({ "X-Hokma-Token": TEST_TOKEN });
    expect(document.querySelectorAll("[data-candle]")).toHaveLength(20);
    expect(screen.getByTestId("opening-line")).toHaveAttribute("data-price", "105");
    expect(screen.getByText(t("chart.opening"))).toBeInTheDocument();
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
    expect(screen.getByTestId("chart-change")).toHaveTextContent("+1.00%".replace(".", ",")); // default locale is Spanish
    expect(screen.getByTestId("market-state")).toHaveTextContent(t("chart.market.open"));
    expect(screen.queryByTestId("delay-notice")).toBeNull();
    expect(screen.getByText(t("chart.source", { source: "Yahoo Finance" }))).toBeInTheDocument();
  });

  it("changing the timeframe requests it again and redraws", async () => {
    serve((url) => {
      const tf = url.searchParams.get("tf") as Timeframe;
      return json(tf === "1D" ? chart({ timeframe: "1D", opening_range: null, candles: chart().candles.slice(0, 8) }) : chart({ timeframe: tf }));
    });
    show();
    await screen.findByTestId("opening-line");
    await userEvent.click(screen.getByRole("button", { name: "1D" }));
    expect(screen.queryByTestId("opening-line")).toBeNull(); // the 5m candles never show under the 1D title
    await waitFor(() => expect(document.querySelectorAll("[data-candle]")).toHaveLength(8));
    expect(requests.at(-1)!.url.searchParams.get("tf")).toBe("1D");
    expect(screen.queryByTestId("opening-line")).toBeNull(); // hidden on the daily chart
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 1D");
    expect(localStorage.getItem("hokma.chart.timeframe")).toBe("1D");
    await userEvent.click(screen.getByRole("button", { name: "15m" }));
    await waitFor(() => expect(requests.at(-1)!.url.searchParams.get("tf")).toBe("15m"));
    await waitFor(() => expect(document.querySelectorAll("[data-candle]")).toHaveLength(20));
  });

  it("says when the market is closed and when the data was last seen", async () => {
    serve(() => json(chart({ market_open: false })));
    show();
    const pill = await screen.findByTestId("market-state");
    expect(pill).toHaveTextContent(t("chart.market.closed"));
    expect(pill.textContent).toMatch(/\d\d:\d\d/);
  });

  it("shows the delay on screen when the source is behind", async () => {
    serve(() => json(chart({ delayed: true, data_age_seconds: 600 })));
    show();
    expect(await screen.findByTestId("delay-notice")).toHaveTextContent(t("chart.delayed", { minutes: 10 }));
  });

  it("warns when the answer comes from saved data", async () => {
    serve(() => json(chart({ stale: true })));
    show();
    expect(await screen.findByTestId("stale-notice")).toBeInTheDocument();
  });

  it("shows the translated backend error with a retry that works", async () => {
    let calls = 0;
    serve(() => (calls++ === 0 ? json({ detail: { code: "network", message: "No network message" } }, 502) : json(chart())));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No network message");
    expect(alert).toHaveTextContent(t("chart.error.title"));
    await userEvent.click(within(alert).getByRole("button", { name: t("ui.error.retry") }));
    await screen.findByTestId("opening-line");
  });

  it("shows an unknown symbol as an error, not as an empty chart", async () => {
    serve(() => json({ detail: { code: "unknown_symbol", message: "Unknown symbol text" } }, 404));
    show({ symbol: "ZZZZ" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown symbol text");
    expect(screen.getByRole("button", { name: t("chart.ask") })).toBeDisabled();
  });

  it("shows an empty state when the source returns no candles", async () => {
    serve(() => json(chart({ candles: [], opening_range: null, last_price: null, change: null, change_pct: null })));
    show();
    expect(await screen.findByText(t("chart.empty.title"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("chart.ask") })).toBeDisabled();
  });

  it("keeps the chart and says so when a refresh fails", async () => {
    let calls = 0;
    serve(() => (calls++ === 0 ? json(chart()) : json({ detail: { code: "network", message: "x" } }, 502)));
    show();
    await screen.findByTestId("opening-line");
    await userEvent.click(screen.getByRole("button", { name: t("chart.refresh") }));
    expect(await screen.findByTestId("refresh-failed")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-candle]")).toHaveLength(20);
  });

  it("refreshes by itself while the market is open", async () => {
    const original = REFRESH_SECONDS["5m"];
    REFRESH_SECONDS["5m"] = 0.05;
    try {
      serve(() => json(chart()));
      show();
      await screen.findByTestId("opening-line");
      await waitFor(() => expect(requests.length).toBeGreaterThanOrEqual(3));
    } finally {
      REFRESH_SECONDS["5m"] = original;
    }
  });

  it("does not refresh by itself when the market is closed", async () => {
    const original = REFRESH_SECONDS["5m"];
    REFRESH_SECONDS["5m"] = 0.05;
    try {
      serve(() => json(chart({ market_open: false })));
      show();
      await screen.findByTestId("market-state");
      await new Promise((resolve) => setTimeout(resolve, 400));
      expect(requests).toHaveLength(1);
    } finally {
      REFRESH_SECONDS["5m"] = original;
    }
  });

  it("without a symbol it asks for one and opens the chart after the pick", async () => {
    serve((url) => {
      if (url.pathname === "/api/market/symbols") return json({ items: [{ symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" }], suggested: true });
      return json(chart({ symbol: "ES=F" }));
    });
    show({});
    expect(screen.getByText(t("chart.none.title"))).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("menuitem", { name: /ES=F/ }));
    expect(await screen.findByTestId("chart-title")).toHaveTextContent("ES=F · 5m");
  });
});

describe("ask the fund about this chart", () => {
  beforeEach(stubCanvas);

  it("uploads the image, sends it in a new conversation and opens the chat tab", async () => {
    const server = new FakeServer();
    serve((url) => (url.pathname === "/api/attachments" ? json({ id: "att-1", name: "chart-NQ-F-5m.png", mime: "image/png", size: 3, kind: "image" }, 201) : json(chart())));
    show();
    await screen.findByTestId("opening-line");

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
    serve((url) => (url.pathname === "/api/attachments" ? json({ id: "att-1", name: "a.png", mime: "image/png", size: 3, kind: "image" }, 201) : json(chart())));
    show();
    await screen.findByTestId("opening-line");
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
    serve((url) => (url.pathname === "/api/attachments" ? json({ detail: { code: "too_big", message: "too big" } }, 413) : json(chart())));
    show();
    await screen.findByTestId("opening-line");
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
