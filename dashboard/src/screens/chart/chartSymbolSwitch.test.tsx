import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setHub } from "@/screens/chat/hub";
import { FakeSocket, installFakeSockets, snapshot } from "@/test/fakeSocket";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN } from "@/test/utils";
import { mountHost, seedTabs } from "./testHost";
import { useFakeCharts } from "./testChartLib";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const HITS = [{ symbol: "ES=F", name: "E-mini S&P 500", kind: "future", exchange: "CME" }];
const bars = (price: number) => Array.from({ length: 30 }, (_, i) => ({ t: 1793802600 + i * 300, o: price, h: price + 2, l: price - 2, c: price + 1, v: 10 }));

beforeEach(() => {
  setLocale("en");
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

async function answer(symbol: string, price: number) {
  const socket = await waitFor(() => FakeSocket.last());
  act(() => socket.serverOpen());
  const sub = (socket.ops("subscribe").at(-1) as { sub: string }).sub;
  act(() => socket.serverSend(snapshot(sub, 1, { symbol, kind: "future", asset_class: "future", bars: bars(price) })));
  return sub;
}

describe("T-11: changing the symbol of a tab", () => {
  it("changes the params and the title, keeps the interval, and never shows the bars of the old symbol under the new title", async () => {
    // Everything the page says about price and title, at every change of the page.
    const seen: Array<{ title: string; price: string }> = [];
    mountHost();
    await answer("NQ=F", 28000);
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "7m{Enter}");
    await answer("NQ=F", 28000);
    expect(screen.getByTestId("chart-price")).toHaveTextContent("28,001.00");
    const watch = new MutationObserver(() => {
      const title = screen.queryByTestId("chart-title")?.textContent ?? "";
      const price = screen.queryByTestId("chart-price")?.textContent ?? "";
      seen.push({ title, price });
    });
    watch.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true });
    try {
      await userEvent.click(screen.getByTestId("chart-symbol"));
      await userEvent.click(await screen.findByRole("option", { name: /ES=F/ }));
      expect(screen.getByLabelText("tab-keys")).toHaveTextContent("chart:symbol=ES=F");
      expect(screen.getByLabelText("active-key")).toHaveTextContent("chart:symbol=ES=F");
      expect(screen.getByTestId("chart-title")).toHaveTextContent("ES=F · 7m");
      expect(screen.queryByTestId("chart-price")).toBeNull(); // nothing of the old symbol while the new one loads
      await answer("ES=F", 5000);
      expect(await screen.findByTestId("chart-price")).toHaveTextContent("5,001.00");
    } finally {
      watch.disconnect();
    }
    const stale = seen.filter((state) => state.title.includes("ES=F") && state.price.includes("28,001"));
    expect(stale).toEqual([]);
    expect(seen.some((state) => state.title.includes("ES=F"))).toBe(true);
  });

  it("the new symbol survives a reload with its interval", async () => {
    const first = mountHost();
    await answer("NQ=F", 28000);
    await userEvent.click(screen.getByRole("button", { name: t("chart.interval.more") }));
    await userEvent.type(await screen.findByRole("combobox"), "7m{Enter}");
    await answer("NQ=F", 28000);
    await userEvent.click(screen.getByTestId("chart-symbol"));
    await userEvent.click(await screen.findByRole("option", { name: /ES=F/ }));
    await answer("ES=F", 5000);
    first.unmount();
    FakeSocket.reset();
    mountHost();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    expect(socket.ops("subscribe").at(-1)).toMatchObject({ symbol: "ES=F", tf: "7m" });
    expect(screen.getByTestId("chart-title")).toHaveTextContent("ES=F · 7m");
  });
});
