import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, getToken, setToken } from "@/api";
import { t as translate } from "@/i18n";
import { FakeSocket, bars, envelope, snapshot } from "@/test/fakeSocket";
import { BACKOFF_SECONDS, DEAD_AFTER_MS, LiveChartClient, PING_EVERY_MS } from "./liveClient";
import { barMode, delayMinutes, errorText, secondsDisabledReason } from "./liveState";
import type { LiveState } from "./liveState";

const TOKEN = "test-session-token-0123456789";
let fetchCalls: string[] = [];
let state: LiveState;
let client: LiveChartClient;

function begin(tf = "5m") {
  client = new LiveChartClient({
    symbol: "BTC-USD",
    timeframe: tf,
    onState: (next) => {
      state = next;
    },
    createSocket: (url) => new FakeSocket(url),
  });
  client.start();
  const socket = FakeSocket.last();
  socket.serverOpen();
  return socket;
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.reset();
  fetchCalls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    fetchCalls.push(String(input));
    return new Response("{}");
  }));
  clearToken();
  setToken(TOKEN);
});

afterEach(() => {
  client?.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("live chart client: subscription", () => {
  it("opens /ws/market with the token and subscribes to the symbol and timeframe", () => {
    const socket = begin();
    const url = new URL(socket.url);
    expect(url.pathname).toBe("/ws/market");
    expect(url.searchParams.get("token")).toBe(TOKEN);
    expect(socket.ops("subscribe")).toEqual([{ op: "subscribe", sub: "c1", symbol: "BTC-USD", tf: "5m" }]);
    expect(state.ready).toBe(false);
  });

  it("paints the snapshot, then bars and prices, and never asks the HTTP API for candles", async () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    expect(state.ready).toBe(true);
    expect(state.bars).toHaveLength(20);
    expect(state.source).toBe("Coinbase");
    const lastBar = state.bars[19]!;
    socket.serverSend(envelope("bar", "c1", 2, { bar: { ...lastBar, c: 150, h: 151 }, final: false, price: 150, ts_ms: 1, change: 50, change_pct: 50 }));
    expect(state.bars).toHaveLength(20);
    expect(state.bars[19]!.c).toBe(150);
    expect(state.price).toBe(150);
    socket.serverSend(envelope("bar", "c1", 3, { bar: { t: lastBar.t + 300, o: 150, h: 152, l: 149, c: 151, v: 1 }, final: true, price: 151, ts_ms: 2, change: 51, change_pct: 51 }));
    expect(state.bars).toHaveLength(21);
    socket.serverSend(envelope("last", "c1", 4, { price: 152, ts_ms: 3, change: 52, change_pct: 52 }));
    expect(state.price).toBe(152);
    expect(state.bars).toHaveLength(21);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(fetchCalls).toEqual([]); // no polling, ever
  });

  it("ignores messages of other subscriptions", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverSend(envelope("last", "zz", 2, { price: 999, ts_ms: 1 }));
    socket.serverSend({ type: "connection_status", provider: "coinbase", epoch: "epoch-aaaa", state: "connected" });
    expect(state.price).not.toBe(999);
  });

  it("shows an error of the subscription and clears it with the next snapshot", () => {
    const socket = begin();
    socket.serverSend({ type: "error", sub: "c1", code: "market.limit", message: "limit", epoch: "epoch-aaaa", source: "none", data_state: "delayed" });
    expect(state.error).toEqual({ code: "market.limit", message: "limit", params: {} });
    client.retry();
    expect(socket.ops("subscribe").at(-1)).toMatchObject({ sub: "c2" });
    socket.serverSend(snapshot("c2", 1));
    expect(state.error).toBeNull();
    expect(state.ready).toBe(true);
  });
});

describe("live chart client: seq and epoch", () => {
  it("ignores a message whose seq is not newer than the last one", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverSend(envelope("last", "c1", 2, { price: 111, ts_ms: 1 }));
    socket.serverSend(envelope("last", "c1", 2, { price: 222, ts_ms: 2 }));
    socket.serverSend(envelope("last", "c1", 1, { price: 333, ts_ms: 3 }));
    expect(state.price).toBe(111);
    expect(socket.ops("subscribe")).toHaveLength(1);
  });

  it("asks for a new snapshot when seq jumps, and ignores the old subscription afterwards", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverSend(envelope("last", "c1", 4, { price: 444, ts_ms: 1 })); // 2 and 3 never arrived
    expect(state.price).not.toBe(444);
    expect(socket.ops("unsubscribe")).toEqual([{ op: "unsubscribe", sub: "c1" }]);
    expect(socket.ops("subscribe").at(-1)).toMatchObject({ sub: "c2", symbol: "BTC-USD", tf: "5m" });
    socket.serverSend(envelope("last", "c1", 5, { price: 555, ts_ms: 2 })); // late message of the old subscription
    expect(state.price).not.toBe(555);
    socket.serverSend(snapshot("c2", 1, { last: 120 }));
    expect(state.price).toBe(120);
    socket.serverSend(envelope("last", "c2", 2, { price: 121, ts_ms: 3 }));
    expect(state.price).toBe(121);
  });

  it("an engine restart (new epoch) drops the old seq: the snapshot of the new run is taken even with a low seq", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverSend(envelope("last", "c1", 2, { price: 111, ts_ms: 1 }));
    socket.serverSend(envelope("last", "c1", 3, { price: 112, ts_ms: 2 }));
    socket.serverSend(snapshot("c1", 1, { last: 130 }, { epoch: "epoch-bbbb" }));
    expect(state.price).toBe(130);
    socket.serverSend(envelope("last", "c1", 2, { price: 131, ts_ms: 3 }, { epoch: "epoch-bbbb" }));
    expect(state.price).toBe(131);
  });

  it("a live message of a new epoch before any snapshot of it asks for the snapshot instead of painting", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverSend(envelope("last", "c1", 2, { price: 777, ts_ms: 1 }, { epoch: "epoch-bbbb" }));
    expect(state.price).not.toBe(777);
    expect(socket.ops("subscribe").at(-1)).toMatchObject({ sub: "c2" });
  });
});

describe("live chart client: dead connections and reconnection", () => {
  it("pings every 15 s and stays connected as long as answers come", async () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    for (let i = 0; i < 8; i += 1) {
      await vi.advanceTimersByTimeAsync(PING_EVERY_MS);
      socket.serverSend({ type: "pong", epoch: "epoch-aaaa" });
    }
    expect(socket.ops("ping")).toHaveLength(8);
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
    expect(state.link).toBe(true);
  });

  it("45 s without any message: the socket is dropped and a new one opens after 1 s, with a new snapshot", async () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    await vi.advanceTimersByTimeAsync(DEAD_AFTER_MS - 1000);
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(socket.closedByPage).toBe(true);
    expect(state.link).toBe(false);
    expect(barMode(state)).toBe("reconnecting");
    expect(state.bars).toHaveLength(20); // what was on screen stays while it reconnects
    await vi.advanceTimersByTimeAsync(999);
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    const next = FakeSocket.last();
    expect(next).not.toBe(socket);
    next.serverOpen();
    expect(next.ops("subscribe")).toEqual([{ op: "subscribe", sub: "c2", symbol: "BTC-USD", tf: "5m" }]);
    next.serverSend(snapshot("c2", 1, { last: 140 }));
    expect(state.link).toBe(true);
    expect(barMode(state)).toBe("live");
  });

  it("waits 1, 2, 4, 8, 16, 30, 30 seconds between failed attempts, and starts again at 1 after a snapshot", async () => {
    expect([...BACKOFF_SECONDS]).toEqual([1, 2, 4, 8, 16, 30]);
    const first = begin();
    first.serverClose();
    for (const wait of [1, 2, 4, 8, 16, 30, 30]) {
      const before = FakeSocket.of("/ws/market").length;
      await vi.advanceTimersByTimeAsync(wait * 1000 - 1);
      expect(FakeSocket.of("/ws/market")).toHaveLength(before);
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeSocket.of("/ws/market")).toHaveLength(before + 1);
      FakeSocket.last().serverClose(); // refused again
    }
    await vi.advanceTimersByTimeAsync(30_000);
    const good = FakeSocket.last();
    good.serverOpen();
    const sub = String((good.ops("subscribe").at(-1) as { sub: string }).sub);
    good.serverSend(snapshot(sub, 1));
    good.serverClose();
    const count = FakeSocket.of("/ws/market").length;
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.of("/ws/market")).toHaveLength(count + 1);
  });

  it("a reconnection never leaves the old socket listening", async () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    socket.serverClose();
    await vi.advanceTimersByTimeAsync(1000);
    FakeSocket.last().serverOpen();
    socket.serverSend(envelope("last", "c1", 2, { price: 888, ts_ms: 1 })); // a zombie
    expect(state.price).not.toBe(888);
  });

  it("a rejected token (4401) clears it and does not retry", async () => {
    const socket = begin();
    socket.serverClose(4401);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(getToken()).toBeNull();
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
  });

  it("stop() ends everything: no timers, no reconnection", async () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    client.stop();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
    expect(socket.closedByPage).toBe(true);
    expect(socket.ops("ping")).toHaveLength(0);
  });
});

describe("live chart client: a delayed value is never painted as live", () => {
  it("data_state delayed wins over a status that says live", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1, { fallback: true, delay_seconds: 600 }, { source: "Yahoo Finance", data_state: "delayed" }));
    expect(barMode(state)).toBe("delayed");
    expect(delayMinutes(state)).toBe(10);
    socket.serverSend(envelope("status", "c1", 2, { state: "live", code: "ok" }, { source: "Yahoo Finance", data_state: "delayed" }));
    expect(barMode(state)).toBe("delayed");
    socket.serverSend(envelope("last", "c1", 3, { price: 101, ts_ms: 1 }, { source: "Yahoo Finance", data_state: "delayed" }));
    expect(barMode(state)).toBe("delayed");
  });

  it("a missing or unknown data_state counts as delayed, not live", () => {
    const socket = begin();
    socket.serverSend({ ...snapshot("c1", 1), data_state: undefined });
    expect(state.dataState).toBe("delayed");
    expect(barMode(state)).toBe("delayed");
  });

  it("live data from a provider shows live, reconnecting and closed take precedence", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1));
    expect(barMode(state)).toBe("live");
    socket.serverSend(envelope("status", "c1", 2, { state: "reconnecting", code: "network" }));
    expect(barMode(state)).toBe("reconnecting");
    socket.serverSend(envelope("status", "c1", 3, { state: "closed", code: "market.closed" }));
    expect(barMode(state)).toBe("closed");
    socket.serverSend(envelope("status", "c1", 4, { state: "live", code: "ok" }));
    expect(barMode(state)).toBe("live");
  });

  it("the bars the snapshot carries are ready to draw", () => {
    const socket = begin();
    socket.serverSend(snapshot("c1", 1, { bars: bars(5) }));
    expect(state.bars).toHaveLength(5);
  });
});

describe("live chart client: free time intervals and seconds", () => {
  it("subscribes with the canonical interval and reads interval, exact_from and approx_until", () => {
    const socket = begin("7m");
    expect(socket.ops("subscribe")).toEqual([{ op: "subscribe", sub: "c1", symbol: "BTC-USD", tf: "7m" }]);
    socket.serverSend(snapshot("c1", 1, { tf: "7m", bars: bars(5, 1793802600, 420), interval: { kind: "time", seconds: 420 }, exact_from: null, approx_until: null }));
    expect(state.interval).toEqual({ kind: "time", seconds: 420 });
    expect(state.exactFrom).toBeNull();
    expect(state.notice).toBeNull();
  });

  it("seconds built from the trades say where they start", () => {
    const socket = begin("15s");
    const start = 1793802600;
    socket.serverSend(snapshot("c1", 1, { bars: bars(4, start, 15), interval: { kind: "time", seconds: 15 }, exact_from: start, approx_until: null }));
    expect(state.notice).toEqual({ kind: "seconds_start", time: start });
    socket.serverSend(envelope("bar", "c1", 2, { bar: { t: start + 60, o: 1, h: 2, l: 0.5, c: 1.5, v: 3 }, final: false, price: 1.5, ts_ms: 1 }));
    expect(state.bars).toHaveLength(5);
    expect(state.notice).toEqual({ kind: "seconds_start", time: start });
  });

  it("seconds without trades: market.seconds_need_ticks, and the interval box disables seconds", () => {
    const socket = begin("15s");
    expect(secondsDisabledReason(state)).toBeNull();
    socket.serverSend({ type: "error", sub: "c1", code: "market.seconds_need_ticks", message: "", epoch: "epoch-aaaa", source: "Yahoo Finance", data_state: "delayed" });
    expect(state.error).toEqual({ code: "market.seconds_need_ticks", message: "", params: {} });
    expect(errorText(state.error!)).toBe("Esta fuente da como mucho barras de 1 minuto. Conecta una fuente en vivo para ver segundos");
    expect(secondsDisabledReason(state)).toBe(errorText(state.error!));
  });

  it("the delayed fallback also disables seconds in the interval box", () => {
    const socket = begin("5m");
    socket.serverSend(snapshot("c1", 1, { fallback: true }, { source: "Yahoo Finance", data_state: "delayed" }));
    expect(secondsDisabledReason(state)).not.toBeNull();
  });

  it("market.interval_unavailable is told with its values, and tolerated without them", () => {
    const socket = begin("13m");
    socket.serverSend({
      type: "error", sub: "c1", code: "market.interval_unavailable", message: "no 1m", epoch: "epoch-aaaa", source: "none", data_state: "delayed",
      params: { interval: "13m", base: "1m", source: "Yahoo Finance", symbol: "BTC-USD", junk: { nested: true } },
    });
    expect(state.error!.params).toEqual({ interval: "13m", base: "1m", source: "Yahoo Finance", symbol: "BTC-USD" });
    expect(errorText(state.error!)).toBe("13m necesita barras de 1m, y Yahoo Finance no las tiene para BTC-USD");
    expect(errorText({ code: "market.interval_unavailable", message: "", params: {} })).toBe("Ese intervalo no est\u00e1 disponible para este s\u00edmbolo");
    expect(errorText({ code: "market.interval_unavailable", message: "server text", params: {} })).toBe("server text");
    expect(errorText({ code: "market.bad_request", message: "x", params: {} })).toBe(translate("market.err.bad_request"));
  });
});

describe("live chart client: range bars", () => {
  const T0 = 1793802600;
  const rangeBar = (i: number, t: number, c: number, approx = false) => ({ i, t, o: c - 1, h: c, l: c - 2.5, c, v: 10 + i, approx });
  const RANGE = { kind: "range", size: 10, unit: "t", tick_size: 0.25 };

  it("subscribes with r:10t, keeps i and approx, and several bars can share one second", () => {
    const socket = begin("r:10t");
    expect(socket.ops("subscribe")).toEqual([{ op: "subscribe", sub: "c1", symbol: "BTC-USD", tf: "r:10t" }]);
    const list = [rangeBar(0, T0, 100, true), rangeBar(1, T0 + 60, 102.5, true), rangeBar(2, T0 + 90, 105), rangeBar(3, T0 + 90, 107.5)];
    socket.serverSend(snapshot("c1", 1, { bars: list, interval: RANGE, exact_from: T0 + 90, approx_until: T0 + 90 }));
    expect(state.interval).toEqual(RANGE);
    expect(state.bars.map((b) => [b.i, b.approx ?? false])).toEqual([[0, true], [1, true], [2, false], [3, false]]);
    expect(state.notice).toEqual({ kind: "approx_before", time: T0 + 90 });
  });

  it("the bar in progress updates the last one by index; a closed bar is followed by the next index", () => {
    const socket = begin("r:10t");
    const list = [rangeBar(0, T0, 100), rangeBar(1, T0 + 30, 102.5)];
    socket.serverSend(snapshot("c1", 1, { bars: list, interval: RANGE, exact_from: T0, approx_until: null }));
    // Same second as bar 1, same index: it is the bar in progress, not a new one.
    socket.serverSend(envelope("bar", "c1", 2, { bar: { i: 1, t: T0 + 30, o: 101.5, h: 103, l: 100.5, c: 103, v: 20 }, final: false, price: 103, ts_ms: 1 }));
    expect(state.bars).toHaveLength(2);
    expect(state.bars[1]!.c).toBe(103);
    socket.serverSend(envelope("bar", "c1", 3, { bar: { i: 1, t: T0 + 30, o: 101.5, h: 103, l: 100.5, c: 103, v: 22 }, final: true, price: 103, ts_ms: 2 }));
    expect(state.bars).toHaveLength(2);
    expect(state.bars[1]!.v).toBe(22);
    // Bar 2 opens in the very same second.
    socket.serverSend(envelope("bar", "c1", 4, { bar: { i: 2, t: T0 + 30, o: 103.25, h: 103.25, l: 103.25, c: 103.25, v: 1 }, final: false, price: 103.25, ts_ms: 3 }));
    expect(state.bars.map((b) => b.i)).toEqual([0, 1, 2]);
    expect(state.notice).toBeNull(); // everything came from trades
    // An old index that is not in view is ignored.
    socket.serverSend(envelope("bar", "c1", 5, { bar: { i: -5, t: T0 - 900, o: 1, h: 1, l: 1, c: 1, v: 1 }, final: true, price: 103.25, ts_ms: 4 }));
    expect(state.bars.map((b) => b.i)).toEqual([0, 1, 2]);
  });

  it("all approximate (no trades): approx_all", () => {
    const socket = begin("r:2.5p");
    expect(socket.ops("subscribe").at(-1)).toMatchObject({ tf: "r:2.5p" });
    socket.serverSend(snapshot("c1", 1, { bars: [rangeBar(0, T0, 100, true), rangeBar(1, T0 + 60, 102.5, true)], interval: { kind: "range", size: 2.5, unit: "p", tick_size: null }, exact_from: null, approx_until: T0 + 120 }, { source: "Yahoo Finance", data_state: "delayed" }));
    expect(state.notice).toEqual({ kind: "approx_all" });
    expect(state.interval).toEqual({ kind: "range", size: 2.5, unit: "p", tick_size: null });
  });

  it("an engine that does not send the new fields yet still paints the bars, with no notice", () => {
    const socket = begin("5m");
    socket.serverSend(snapshot("c1", 1));
    expect(state.interval).toBeNull();
    expect(state.exactFrom).toBeNull();
    expect(state.approxUntil).toBeNull();
    expect(state.notice).toBeNull();
    expect(state.bars).toHaveLength(20);
  });
});
