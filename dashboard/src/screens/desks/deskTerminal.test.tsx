import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { FakeSocket, bars, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { FakeServer } from "../chat/testkit";
import { setHub } from "../chat/hub";
import { useFakeCharts } from "../chart/testChartLib";
import { DesksScreen } from "./DesksScreen";
import { resetDesksStore } from "./store";
import type { DeskSummary } from "./types";

/** The long dash, built by code so this file stays free of it. */
const DASH = String.fromCharCode(0x2014);

/** Opens the socket of the chart on screen and answers its last subscription with a snapshot of `symbol`. */
async function connect(symbol: string, tf = "5m") {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, { symbol, tf, bars: bars(12) })));
  return { socket, sub };
}

const subscribed = () => FakeSocket.instances.flatMap((s) => s.ops("subscribe"));

function desk(patch: Partial<DeskSummary> = {}): DeskSummary {
  return {
    id: 7, project_id: 10, name: "Futuros NQ", focus: "Intraday futures", capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 3, agents_working: 1, tokens_week: 0, token_budget: 300000, budget_state: "ok", ...patch,
  };
}

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

interface Server {
  urls: string[];
  assets: string[];
  detailFails: boolean;
  quoteFails: Set<string>;
  last: Record<string, number>;
}

function stubServer(over: Partial<Server> = {}): Server {
  const server: Server = { urls: [], assets: ["NQ=F", "ES=F"], detailFails: false, quoteFails: new Set(), last: { "NQ=F": 21430.25, "ES=F": 6120.5 }, ...over };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = String(url);
      server.urls.push(path);
      if (path === "/api/desks") return json({ desks: [desk()] });
      if (path === "/api/desks/7/conversation") return json({ id: "c1", agent_slug: "desk-chief", model: "sonnet" });
      if (path === "/api/desks/7") {
        return server.detailFails ? json({ detail: { code: "x", message: "boom" } }, 500) : json({ id: 7, name: "Futuros NQ", capital: 10000, assets: server.assets });
      }
      if (path.startsWith("/api/market/candles")) {
        const symbol = decodeURIComponent(new URL(path, "http://x").searchParams.get("symbol") ?? "");
        if (server.quoteFails.has(symbol)) return json({ detail: { code: "market.down", message: "down" } }, 502);
        const last = server.last[symbol] ?? 1;
        return json({ symbol, candles: [{ t: 1, o: 1, h: 2, l: 1, c: last - 1, v: 1 }, { t: 2, o: 1, h: 2, l: 1, c: last, v: 1 }], last_price: last, previous_close: last - 10, change: 10, change_pct: symbol === "NQ=F" ? 0.5 : -0.25 });
      }
      return json({});
    }),
  );
  return server;
}

const candleCalls = (server: Server, symbol: string) => server.urls.filter((u) => u.startsWith("/api/market/candles") && u.includes(encodeURIComponent(symbol)));

function open() {
  setToken(TEST_TOKEN);
  return renderWithApp(<DesksScreen tabKey="desks:7" params={{ desk: "7", tab: "terminal" }} active />);
}

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
  useFakeCharts(); // jsdom has no canvas
  installFakeSockets();
  new FakeServer(); // the chat socket of the chief column is a stand-in, so the chart sockets stay the only fake sockets
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setHub(null);
});

describe("terminal of the desk", () => {
  it("lays out the list, the chart, the chief column and the positions panel, each with its content", async () => {
    stubServer();
    open();
    await connect("NQ=F");
    const list = screen.getByLabelText(t("desks.terminal.list"));
    expect(within(list).getAllByRole("row")).toHaveLength(3); // header + 2 assets
    const nq = within(list).getByRole("button", { name: "NQ=F" }).closest("tr")!;
    const es = within(list).getByRole("button", { name: "ES=F" }).closest("tr")!;
    await waitFor(() => expect(within(nq).getByText("21.430,25")).toBeInTheDocument());
    expect(within(nq).getByText("+0,50%")).toBeInTheDocument();
    await waitFor(() => expect(within(es).getByText("6120,50")).toBeInTheDocument()); // es groups thousands from five digits
    expect(within(es).getByText("-0,25%")).toBeInTheDocument();
    expect(screen.getByText(t("desks.terminal.prevClose", { value: "21.420,25" }))).toBeInTheDocument();
    // chart of the first asset
    expect(await screen.findByRole("img", { name: t("chart.aria", { symbol: "NQ=F", tf: "5m" }) })).toBeInTheDocument();
    expect(screen.getByText("NQ=F · 5m")).toBeInTheDocument();
    // chief column: the real chat of the desk, with its message box
    const chat = screen.getByLabelText(t("desks.terminal.chat"), { selector: ".pane" });
    expect(await within(chat).findByRole("textbox", { name: t("desks.chat.input") })).toBeEnabled();
    // positions panel
    const positions = screen.getByLabelText(t("desks.terminal.positions"), { selector: ".pane" });
    expect(within(positions).getByText(t("desks.terminal.positions.title"))).toBeInTheDocument();
    expect(within(positions).getByText(t("desks.terminal.positions.when"))).toBeInTheDocument();
    // grid placement: the chief column and the positions panel are cells of the same grid
    expect(chat.className).toContain("dterm-chat");
    expect(positions.className).toContain("dterm-positions");
    expect(chat.parentElement).toBe(positions.parentElement);
    expect(list.parentElement).toBe(chat.parentElement);
    expect(screen.getByText(t("chart.source", { source: "Coinbase" }))).toBeInTheDocument();
  });

  it("moves the chart to the asset the student picks", async () => {
    stubServer();
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    await user.click(screen.getByRole("button", { name: "ES=F" }));
    expect(screen.getByRole("button", { name: "ES=F" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "NQ=F" })).toHaveAttribute("aria-pressed", "false");
    await connect("ES=F");
    expect(subscribed().at(-1)).toMatchObject({ symbol: "ES=F", tf: "5m" });
    expect(await screen.findByRole("img", { name: t("chart.aria", { symbol: "ES=F", tf: "5m" }) })).toBeInTheDocument();
    expect(screen.getByText(t("desks.terminal.quote", { symbol: "ES=F" }))).toBeInTheDocument();
  });

  it("changes the interval of the chart without touching the list", async () => {
    const server = stubServer();
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    await screen.findByText("21.430,25", { selector: "td" });
    const before = candleCalls(server, "NQ=F").length;
    await user.click(screen.getByRole("button", { name: "15m" }));
    await connect("NQ=F", "15m");
    expect(subscribed().at(-1)).toMatchObject({ symbol: "NQ=F", tf: "15m" });
    expect(screen.getByText("NQ=F · 15m")).toBeInTheDocument();
    expect(candleCalls(server, "NQ=F")).toHaveLength(before);
  });

  it("reads the quotes again every 60 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = stubServer();
    open();
    await screen.findByText("21.430,25", { selector: "td" });
    expect(candleCalls(server, "NQ=F")).toHaveLength(1);
    server.last["NQ=F"] = 21500;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(candleCalls(server, "NQ=F")).toHaveLength(2);
    expect(await screen.findByText("21.500,00", { selector: "td" })).toBeInTheDocument();
  });

  it("shows a dash for an asset whose quote failed and keeps the rest", async () => {
    stubServer({ quoteFails: new Set(["ES=F"]) });
    open();
    await screen.findByText("21.430,25", { selector: "td" });
    const row = screen.getByRole("button", { name: "ES=F" }).closest("tr")!;
    await waitFor(() => expect(within(row).getAllByText("-")).toHaveLength(2));
  });

  it("says so when the desk has no assets", async () => {
    stubServer({ assets: [] });
    open();
    expect(await screen.findByText(t("desks.terminal.noAssets.title"))).toBeInTheDocument();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("offers a retry when the desk cannot be read", async () => {
    const server = stubServer({ detailFails: true });
    open();
    const user = userEvent.setup();
    expect(await screen.findByText(t("desks.terminal.error"))).toBeInTheDocument();
    server.detailFails = false;
    await user.click(screen.getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByRole("button", { name: "NQ=F" })).toBeInTheDocument();
  });

  it("shows the chart error with a retry that subscribes again", async () => {
    stubServer();
    open();
    const user = userEvent.setup();
    const { socket, sub } = await connect("NQ=F");
    act(() => socket.serverSend({ type: "error", sub, code: "market.limit", message: "x", epoch: "epoch-aaaa", source: "none", data_state: "delayed" }));
    const alert = await screen.findByTestId("desk-chart-error");
    expect(alert).toHaveAttribute("data-code", "market.limit");
    await user.click(within(alert).getByRole("button", { name: t("ui.error.retry") }));
    expect(socket.ops("subscribe")).toHaveLength(2);
  });

  it("waits for the first snapshot with a loading state", async () => {
    stubServer();
    open();
    expect(await screen.findByText(t("chart.loading"))).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /NQ=F/ })).not.toBeInTheDocument();
  });

  it("has every text in Spanish and English and none with the long dash", async () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const keys = Object.keys(es).filter((k) => k.startsWith("desks.terminal."));
    expect(keys.length).toBeGreaterThan(15);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key]).not.toContain(DASH);
      expect(en[key]).not.toContain(DASH);
    }
    expect(Object.keys(en).filter((k) => k.startsWith("desks.terminal."))).toEqual(keys);
    setLocale("en");
    stubServer();
    open();
    expect(await screen.findByText("Watching")).toBeInTheDocument();
    expect(screen.getByText("No positions yet")).toBeInTheDocument();
  });
});
