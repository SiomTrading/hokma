import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setHub } from "@/screens/chat/hub";
import { FakeSocket, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { t } from "@/i18n";
import { TEST_TOKEN } from "@/test/utils";
import { mountHost, seedTabs } from "./testHost";
import { useFakeCharts } from "./testChartLib";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

const HITS = [
  { symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" },
  { symbol: "AAPL", name: "Apple", kind: "equity", exchange: "NASDAQ" },
];

beforeEach(() => {
  useFakeCharts();
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(async () => json({ items: HITS, suggested: true })));
  seedTabs([{ key: "chart:symbol=NQ=F", params: { symbol: "NQ=F" } }]);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setHub(null);
});

/** Answers the last subscription of the newest socket. */
async function connect(fields: Record<string, unknown> = {}) {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, fields)));
  return { socket, sub };
}

const subscribes = () => FakeSocket.of("/ws/market").flatMap((s) => s.ops("subscribe"));

describe("chart header (3060 wiring)", () => {
  it("shows the symbol as a button and the interval; the favorites are the old five", async () => {
    mountHost();
    await connect();
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
    expect(screen.getByTestId("chart-symbol").tagName).toBe("BUTTON");
    const favorites = screen.getByRole("group", { name: t("chart.timeframe") });
    expect(favorites.textContent).toBe("1m5m15m1h1D");
  });

  it("writes any interval in the box and subscribes with it", async () => {
    mountHost();
    await connect();
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    const field = await screen.findByRole("combobox");
    await userEvent.type(field, "7m{Enter}");
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 7m");
    expect(screen.queryByRole("dialog")).toBeNull();
    await connect();
    expect(subscribes().at(-1)).toMatchObject({ symbol: "NQ=F", tf: "7m" });
  });

  it("says why an interval is rejected and keeps the current one", async () => {
    mountHost();
    await connect();
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "3M{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent(t("chart.interval.months"));
    expect(screen.getByTestId("chart-title")).toHaveTextContent("NQ=F · 5m");
  });

  it("changes the asset of the tab from the title: the tab key follows and the interval stays", async () => {
    mountHost();
    await connect();
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "7m{Enter}");
    await userEvent.click(screen.getByTestId("chart-symbol"));
    await userEvent.click(await screen.findByRole("option", { name: /ES=F/ }));
    expect(screen.getByLabelText("tab-keys")).toHaveTextContent("chart:symbol=ES=F");
    expect(screen.getByLabelText("active-key")).toHaveTextContent("chart:symbol=ES=F");
    await connect();
    expect(subscribes().at(-1)).toMatchObject({ symbol: "ES=F", tf: "7m" });
    expect(screen.getByTestId("chart-title")).toHaveTextContent("ES=F · 7m");
  });

  describe("keyboard", () => {
    async function focusBody() {
      await connect();
      const body = await screen.findByTestId("chart-body");
      body.focus();
      return body;
    }

    it("a digit opens the interval box with it written", async () => {
      mountHost();
      await focusBody();
      await userEvent.keyboard("7");
      const field = await screen.findByRole("combobox");
      expect(field).toHaveValue("7");
    });

    it("a letter opens the search with it written, and Ctrl+K opens it empty", async () => {
      mountHost();
      const body = await focusBody();
      await userEvent.keyboard("e");
      expect(await screen.findByRole("combobox")).toHaveValue("e");
      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(document.activeElement).toBe(body); // the focus goes back to the chart
      fireEvent.keyDown(body, { key: "k", ctrlKey: true });
      expect(await screen.findByRole("combobox")).toHaveValue("");
    });

    it("typing inside a field never triggers a shortcut", async () => {
      mountHost();
      await focusBody();
      await userEvent.keyboard("7");
      const field = await screen.findByRole("combobox");
      await userEvent.type(field, "m");
      expect(field).toHaveValue("7m");
      expect(screen.getAllByRole("dialog")).toHaveLength(1);
    });
  });
});
