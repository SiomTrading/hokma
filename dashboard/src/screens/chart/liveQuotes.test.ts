import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN } from "@/test/utils";
import { LiveQuotesClient, type LiveQuote } from "./liveQuotes";

beforeEach(() => {
  vi.useFakeTimers();
  clearToken();
  setToken(TEST_TOKEN);
  installFakeSockets();
});

function client(symbols: string[]) {
  const quotes: Array<[string, unknown]> = [];
  const failed: string[] = [];
  const c = new LiveQuotesClient({ symbols, onQuote: (s, q) => quotes.push([s, q]), onFailed: (s) => failed.push(s) });
  return { c, quotes, failed };
}

describe("the prices of a list by stream", () => {
  it("opens no socket for an empty list and one socket with a subscription per symbol otherwise", () => {
    client([]).c.start();
    expect(FakeSocket.of("/ws/market")).toHaveLength(0);
    const { c } = client(["NQ=F", "ES=F"]);
    c.start();
    const socket = FakeSocket.last();
    socket.serverOpen();
    expect(socket.ops("subscribe").map((o) => [o.symbol, o.tf])).toEqual([["NQ=F", "1D"], ["ES=F", "1D"]]);
    c.stop();
  });

  it("takes the snapshot and then each price message, and tells a refused symbol", () => {
    const { c, quotes, failed } = client(["NQ=F", "ES=F"]);
    c.start();
    const socket = FakeSocket.last();
    socket.serverOpen();
    const [nq, es] = socket.ops("subscribe").map((o) => String(o.sub));
    socket.serverSend({ type: "snapshot", sub: nq, seq: 1, epoch: "e1", last: 100, previous_close: 90, change_pct: 11.1 });
    socket.serverSend({ type: "last", sub: nq, seq: 2, epoch: "e1", price: 101, change_pct: 12.2 });
    socket.serverSend({ type: "error", sub: es, seq: 1, epoch: "e1", code: "market.down" });
    expect(quotes).toEqual([["NQ=F", { last: 100, previousClose: 90, changePct: 11.1, volume: null, source: null, delayed: false }], ["NQ=F", { last: 101, previousClose: null, changePct: 12.2, volume: null, source: null, delayed: false }]]);
    socket.serverSend({ type: "last", sub: nq, seq: 3, epoch: "e1", price: 102, change_pct: 13, source: "Schwab", data_state: "live" });
    socket.serverSend({ type: "last", sub: nq, seq: 4, epoch: "e1", price: 103, change_pct: 14, source: "Yahoo Finance", data_state: "delayed" });
    expect((quotes.slice(2) as Array<[string, LiveQuote]>).map(([, q]) => [q.source, q.delayed])).toEqual([["Schwab", false], ["Yahoo Finance", true]]);
    expect(failed).toEqual(["ES=F"]);
    c.stop();
  });

  it("after a drop waits 1 second, opens another socket and asks every symbol again; a new epoch asks again too", () => {
    const { c } = client(["NQ=F"]);
    c.start();
    const first = FakeSocket.last();
    first.serverOpen();
    first.serverClose(1011);
    expect(FakeSocket.of("/ws/market")).toHaveLength(1);
    vi.advanceTimersByTime(1000);
    const second = FakeSocket.last();
    expect(second).not.toBe(first);
    second.serverOpen();
    expect(second.ops("subscribe")).toHaveLength(1);
    second.serverSend({ type: "connection_status", provider: "schwab", epoch: "e1" });
    second.serverSend({ type: "connection_status", provider: "schwab", epoch: "e2" });
    expect(second.ops("subscribe")).toHaveLength(2);
    c.stop();
  });
});
