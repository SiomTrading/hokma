import { vi } from "vitest";
import type { SocketLike } from "@/screens/chart/liveClient";

/** A WebSocket stand-in. The page side is the usual API; the `server*` methods play the engine. */
export class FakeSocket implements SocketLike {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeSocket[] = [];

  readonly url: string;
  readyState = 0;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number; reason?: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  sent: Array<Record<string, unknown>> = [];
  closedByPage = false;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  static reset(): void {
    FakeSocket.instances = [];
  }

  /** Sockets opened so far for a path such as `/ws/market`. */
  static of(path: string): FakeSocket[] {
    return FakeSocket.instances.filter((s) => new URL(s.url).pathname === path);
  }

  static last(path = "/ws/market"): FakeSocket {
    const list = FakeSocket.of(path);
    const socket = list[list.length - 1];
    if (!socket) throw new Error("no socket opened for " + path);
    return socket;
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(): void {
    this.closedByPage = true;
    this.readyState = 3;
  }

  ops(op: string): Array<Record<string, unknown>> {
    return this.sent.filter((m) => m.op === op);
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  serverSend(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  serverClose(code = 1011): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

export function installFakeSockets(): void {
  FakeSocket.reset();
  vi.stubGlobal("WebSocket", FakeSocket);
}

/** Messages of the engine, with the fields every one carries. */
export const EPOCH = "epoch-aaaa";

export function envelope(type: string, sub: string, seq: number, fields: Record<string, unknown> = {}, extra: { epoch?: string; source?: string; data_state?: string } = {}) {
  return { type, sub, seq, epoch: extra.epoch ?? EPOCH, source: extra.source ?? "Coinbase", data_state: extra.data_state ?? "live", ...fields };
}

export function bars(count = 20, start = 1793802600, step = 300) {
  return Array.from({ length: count }, (_, i) => {
    const base = 100 + Math.sin(i) * 3;
    return { t: start + i * step, o: base, h: base + 2, l: base - 2, c: base + (i % 2 ? -1 : 1), v: 1000 + i };
  });
}

export function snapshot(sub: string, seq = 1, fields: Record<string, unknown> = {}, extra: { epoch?: string; source?: string; data_state?: string } = {}) {
  const list = (fields.bars as ReturnType<typeof bars> | undefined) ?? bars();
  return envelope(
    "snapshot",
    sub,
    seq,
    {
      symbol: "BTC-USD", tf: "5m", name: "Bitcoin USD", kind: "crypto", exchange: "Coinbase", currency: "USD", timezone: "UTC",
      asset_class: "crypto", fallback: false, bars: list, last: list.length ? list[list.length - 1]!.c : null, previous_close: 100, change: 1, change_pct: 1,
      opening_range: null, market_open: true, delay_seconds: 0, ...fields,
    },
    extra,
  );
}
