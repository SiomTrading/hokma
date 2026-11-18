import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
    expect(within(positions).getByText("Sin posiciones abiertas")).toBeInTheDocument();
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

  it("picks an asset with the keyboard: arrows move the focus, Enter picks it, the row is highlighted", async () => {
    stubServer();
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    const nq = screen.getByRole("button", { name: "NQ=F" });
    const es = screen.getByRole("button", { name: "ES=F" });
    nq.focus();
    await user.keyboard("{ArrowDown}");
    expect(es).toHaveFocus();
    expect(es).toHaveAttribute("aria-pressed", "true");
    expect(es.closest("tr")).toHaveClass("on");
    expect(nq.closest("tr")).not.toHaveClass("on");
    expect(screen.getByText("ES=F · 5m")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}"); // past the last one: stays
    expect(es).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(nq).toHaveFocus();
    expect(nq).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByText("ES=F · 5m")).toBeInTheDocument();
    expect(screen.getByText(t("desks.terminal.quote", { symbol: "ES=F" }))).toBeInTheDocument();
  });

  it("remembers the asset in focus for the desk", async () => {
    stubServer();
    const first = open();
    const user = userEvent.setup();
    await connect("NQ=F");
    await user.click(screen.getByRole("button", { name: "ES=F" }));
    first.unmount();
    FakeSocket.instances.length = 0;
    open();
    await screen.findByRole("button", { name: "ES=F" });
    expect(screen.getByRole("button", { name: "ES=F" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("ES=F · 5m")).toBeInTheDocument();
  });

  it("keeps the watchlist inside its own cell so the positions panel never covers an asset", () => {
    // jsdom has no layout: this guards the rule that made the second asset unclickable on a short window.
    const css = readFileSync(resolve(__dirname, "desks.css"), "utf8");
    const rule = (selector: string) => css.split("\n").find((line) => line.startsWith(selector + " {")) ?? "";
    expect(rule(".dterm-list")).toMatch(/min-height:\s*0/);
    expect(rule(".dterm-list")).toMatch(/overflow:\s*hidden/);
    expect(rule(".dterm-rows")).toMatch(/overflow-y:\s*auto/);
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

  it("offers every timeframe of the Chart screen, takes typed ones, and remembers each asset's own", async () => {
    stubServer();
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    const bar = screen.getByRole("group", { name: t("chart.timeframe") });
    expect(within(bar).getAllByRole("button").map((b) => b.textContent)).toEqual(["1m", "5m", "15m", "1h", "1D", "1W"]);
    await user.click(within(bar).getByRole("button", { name: "1W" }));
    await connect("NQ=F", "1W");
    expect(subscribed().at(-1)).toMatchObject({ symbol: "NQ=F", tf: "1W" });
    expect(screen.getByText("NQ=F · 1W")).toBeInTheDocument();
    for (const typed of ["3h", "1W"]) {
      await user.click(screen.getByRole("button", { name: t("chart.interval.more") }));
      await user.type(screen.getByRole("combobox", { name: t("chart.interval.label") }), `${typed}{Enter}`);
      await connect("NQ=F", typed);
      expect(subscribed().at(-1)).toMatchObject({ symbol: "NQ=F", tf: typed });
      expect(screen.getByText(`NQ=F · ${typed}`)).toBeInTheDocument();
    }
    await user.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await user.type(screen.getByRole("combobox", { name: t("chart.interval.label") }), "3h{Enter}");
    await connect("NQ=F", "3h");
    // ES=F has none of its own yet: the default; going back to NQ=F finds 3h
    await user.click(screen.getByRole("button", { name: "ES=F" }));
    await connect("ES=F");
    expect(subscribed().at(-1)).toMatchObject({ symbol: "ES=F", tf: "5m" });
    await user.click(screen.getByRole("button", { name: "NQ=F" }));
    await connect("NQ=F", "3h");
    expect(subscribed().at(-1)).toMatchObject({ symbol: "NQ=F", tf: "3h" });
  });

  it("refuses a bad interval in the Terminal chart with the same message as the Chart screen", async () => {
    stubServer();
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    const before = subscribed().length;
    await user.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await user.type(screen.getByRole("combobox", { name: t("chart.interval.label") }), "abc{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(t("chart.interval.invalid"));
    expect(subscribed()).toHaveLength(before);
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

  it("with no assets still shows the list with its add button, the invitation and the chief chat", async () => {
    stubServer({ assets: [] });
    open();
    expect(await screen.findByText(t("desks.terminal.noAssets.title"))).toBeInTheDocument();
    const list = screen.getByLabelText(t("desks.terminal.list"));
    expect(within(list).getByRole("button", { name: t("desks.terminal.add") })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: t("desks.terminal.add") })).toHaveLength(2); // list and center
    const chat = screen.getByLabelText(t("desks.terminal.chat"), { selector: ".pane" });
    expect(await within(chat).findByRole("textbox", { name: t("desks.chat.input") })).toBeEnabled();
    expect(screen.getByText("Sin posiciones abiertas")).toBeInTheDocument();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("adds an asset from the Terminal: it is saved on the desk, listed and in focus", async () => {
    const server = stubServer({ assets: [] });
    const saved: string[][] = [];
    const base = globalThis.fetch as unknown as (url: string, init?: RequestInit) => Promise<Response>;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const path = String(url);
        if (path === "/api/desks/7/assets" && init?.method === "PUT") {
          const body = JSON.parse(String(init.body)) as { assets: string[] };
          saved.push(body.assets);
          server.assets = body.assets;
          return json({ assets: body.assets });
        }
        if (path.startsWith("/api/market/symbols")) return json({ items: [{ symbol: "NQ=F", name: "Nasdaq 100 futures", exchange: "CME", kind: "future" }], suggested: true, live_classes: [] });
        return base(path, init);
      }),
    );
    open();
    const user = userEvent.setup();
    await screen.findByText(t("desks.terminal.noAssets.title"));
    const center = screen.getByLabelText(t("desks.terminal.chartEmpty"));
    await user.click(within(center).getByRole("button", { name: t("desks.terminal.add") }));
    await user.click(await screen.findByText("Nasdaq 100 futures"));
    await waitFor(() => expect(saved).toEqual([["NQ=F"]]));
    const list = screen.getByLabelText(t("desks.terminal.list"));
    const row = (await within(list).findByRole("button", { name: "NQ=F" })).closest("tr")!;
    expect(row).toHaveClass("on");
    await connect("NQ=F");
    expect(await screen.findByText("NQ=F · 5m")).toBeInTheDocument();
  });

  it("shows the server's reason when the asset cannot be saved", async () => {
    stubServer({ assets: [] });
    const base = globalThis.fetch as unknown as (url: string, init?: RequestInit) => Promise<Response>;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const path = String(url);
        if (path === "/api/desks/7/assets" && init?.method === "PUT") return json({ detail: { code: "desks.asset", message: "Unknown symbol ZZZ" } }, 422);
        if (path.startsWith("/api/market/symbols")) return json({ items: [{ symbol: "ZZZ", name: "Zed", exchange: "X", kind: "equity" }], suggested: true, live_classes: [] });
        return base(path, init);
      }),
    );
    open();
    const user = userEvent.setup();
    await screen.findByText(t("desks.terminal.noAssets.title"));
    await user.click(within(screen.getByLabelText(t("desks.terminal.list"))).getByRole("button", { name: t("desks.terminal.add") }));
    await user.click(await screen.findByText("Zed"));
    expect(await screen.findByText("Unknown symbol ZZZ")).toBeInTheDocument();
    expect(screen.getByText(t("desks.terminal.noAssets.title"))).toBeInTheDocument();
  });

  it("removes an asset from En seguimiento after a short confirmation", async () => {
    const server = stubServer();
    const saved: string[][] = [];
    const base = globalThis.fetch as unknown as (url: string, init?: RequestInit) => Promise<Response>;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url) === "/api/desks/7/assets" && init?.method === "PUT") {
          const body = JSON.parse(String(init.body)) as { assets: string[] };
          saved.push(body.assets);
          server.assets = body.assets;
          return json({ assets: body.assets });
        }
        return base(url, init);
      }),
    );
    open();
    const user = userEvent.setup();
    await connect("NQ=F");
    const list = screen.getByLabelText(t("desks.terminal.list"));
    await user.click(within(list).getByRole("button", { name: t("desks.field.asset.remove", { symbol: "ES=F" }) }));
    await user.click(within(list).getByRole("button", { name: t("desks.terminal.remove.no") })); // cancel keeps it
    expect(saved).toEqual([]);
    expect(within(list).getByRole("button", { name: "ES=F" })).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: t("desks.field.asset.remove", { symbol: "ES=F" }) }));
    expect(saved).toEqual([]); // asking is not removing
    await user.click(within(list).getByRole("button", { name: t("desks.terminal.remove.yes") }));
    await waitFor(() => expect(saved).toEqual([["NQ=F"]]));
    await waitFor(() => expect(within(list).queryByRole("button", { name: "ES=F" })).not.toBeInTheDocument());
    expect(within(list).getByRole("button", { name: "NQ=F" })).toBeInTheDocument();
  });

  it("resizes the chat with the splitter, within 300 px and half the screen, and remembers it per desk", async () => {
    stubServer();
    vi.stubGlobal("innerWidth", 1200);
    vi.stubGlobal("PointerEvent", MouseEvent); // jsdom has no pointer events; a mouse event carries the same coordinates
    const view = open();
    await connect("NQ=F");
    const grid = () => document.querySelector<HTMLElement>(".dterm")!;
    const bar = screen.getByRole("separator", { name: t("desks.terminal.resize") });
    expect(grid().style.getPropertyValue("--chat-w")).toBe("380px");
    expect(bar).toHaveAttribute("aria-valuemin", "300");
    expect(bar).toHaveAttribute("aria-valuemax", "600");
    const drag = (from: number, to: number) => {
      fireEvent.pointerDown(bar, { clientX: from });
      fireEvent(window, new MouseEvent("pointermove", { clientX: to }));
      fireEvent(window, new MouseEvent("pointerup", { clientX: to }));
    };
    drag(800, 700); // 100 px to the left: wider
    expect(grid().style.getPropertyValue("--chat-w")).toBe("480px");
    expect(localStorage.getItem("hokma.desk.7.chatWidth")).toBe("480");
    drag(800, 0); // far too wide: stops at half of the screen
    expect(grid().style.getPropertyValue("--chat-w")).toBe("600px");
    drag(0, 900); // far too narrow: stops at 300
    expect(grid().style.getPropertyValue("--chat-w")).toBe("300px");
    fireEvent.keyDown(bar, { key: "ArrowLeft" });
    expect(grid().style.getPropertyValue("--chat-w")).toBe("320px");
    expect(localStorage.getItem("hokma.desk.7.chatWidth")).toBe("320");
    view.unmount();
    open(); // another visit to the same desk
    await connect("NQ=F");
    expect(document.querySelector<HTMLElement>(".dterm")!.style.getPropertyValue("--chat-w")).toBe("320px");
  });

  it("starts at 380 px and ignores a width that cannot be read or is out of range", async () => {
    stubServer();
    vi.stubGlobal("innerWidth", 1200);
    localStorage.setItem("hokma.desk.7.chatWidth", "9999");
    open();
    await connect("NQ=F");
    expect(document.querySelector<HTMLElement>(".dterm")!.style.getPropertyValue("--chat-w")).toBe("600px");
  });

  it("works even if the browser refuses to store the preferences", async () => {
    stubServer();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    open();
    await connect("NQ=F");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: t("desks.terminal.positions.fold") }));
    expect(document.querySelector(".dterm")).toHaveClass("folded");
    vi.restoreAllMocks();
  });

  it("folds Positions down with its chevron and the desk remembers it", async () => {
    stubServer();
    const view = open();
    const user = userEvent.setup();
    await connect("NQ=F");
    const positions = () => screen.getByLabelText(t("desks.terminal.positions"), { selector: ".pane" });
    expect(within(positions()).getByText(t("desks.terminal.positions.title"))).toBeInTheDocument();
    expect(document.querySelector(".dterm")).not.toHaveClass("folded");
    await user.click(screen.getByRole("button", { name: t("desks.terminal.positions.fold") }));
    expect(document.querySelector(".dterm")).toHaveClass("folded");
    expect(within(positions()).queryByText(t("desks.terminal.positions.title"))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.terminal.positions.unfold") })).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem("hokma.desk.7.positionsFolded")).toBe("1");
    view.unmount();
    open();
    await connect("NQ=F");
    expect(document.querySelector(".dterm")).toHaveClass("folded");
    await user.click(screen.getByRole("button", { name: t("desks.terminal.positions.unfold") }));
    expect(document.querySelector(".dterm")).not.toHaveClass("folded");
    expect(localStorage.getItem("hokma.desk.7.positionsFolded")).toBe("0");
  });

  it("has no step labels nor next-version promises in the Terminal or Positions", async () => {
    stubServer();
    open();
    await connect("NQ=F");
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/paso \d|pr.xima versi.n|step \d|next version/i);
    for (const catalog of [getCatalog("es"), getCatalog("en")]) {
      for (const [key, value] of Object.entries(catalog)) {
        if (key.startsWith("desks.terminal.") || key.startsWith("desks.chat.")) expect(value, key).not.toMatch(/paso \d|pr.xima versi.n|step \d|next version/i);
      }
    }
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
    expect(screen.getByText("No open positions")).toBeInTheDocument();
  });
});
