import { clearToken, getToken, socketUrl } from "@/api";
import type { SocketLike } from "./liveClient";

export const QUOTE_BACKOFF_SECONDS = [1, 2, 4, 8, 16, 30] as const;
const UNAUTHORIZED = 4401;

/** Last price and change of one symbol, as the engine sends them. */
export interface LiveQuote {
  last: number | null;
  previousClose: number | null;
  changePct: number | null;
  /** The provider that sent it, by name (Schwab, SiomTrading Futures, Yahoo Finance...). Null when the message does not say. */
  source: string | null;
  /** The provider says the data is delayed. */
  delayed: boolean;
}

export interface LiveQuotesOptions {
  symbols: readonly string[];
  /** A quote of a symbol (the snapshot, then every message of its price). */
  onQuote: (symbol: string, quote: LiveQuote) => void;
  /** The engine refused the symbol or the source is down: the list shows a dash, never a made-up number. */
  onFailed: (symbol: string) => void;
  createSocket?: (url: string) => SocketLike;
}

const origin = (message: Record<string, unknown>): { source: string | null; delayed: boolean } => ({
  source: typeof message.source === "string" && message.source !== "" && message.source !== "none" ? message.source : null,
  delayed: message.data_state === "delayed",
});

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/**
 * The prices of a list of symbols over ONE `/ws/market` socket, one subscription each. Nothing asks the HTTP API for prices and nothing runs on a timer:
 * the engine sends the snapshot and then each change. A lost socket is opened again with a wait of 1, 2 ... 30 seconds and every symbol is asked again.
 */
export class LiveQuotesClient {
  private socket: SocketLike | null = null;
  private stopped = true;
  private generation = 0;
  private subs = new Map<string, string>();      // sub id -> symbol
  private counter = 0;
  private epoch: string | null = null;
  private attempt = 0;
  private retry: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: LiveQuotesOptions) {}

  start(): void {
    if (!this.stopped || this.options.symbols.length === 0) return;
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.generation += 1;
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    this.detach();
  }

  private detach(): void {
    const socket = this.socket;
    this.socket = null;
    this.subs.clear();
    if (!socket) return;
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    try {
      socket.close();
    } catch {
      /* already closed */
    }
  }

  private open(): void {
    const token = getToken();
    if (!token) return;
    const mine = (this.generation += 1);
    const create = this.options.createSocket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
    const socket = create(socketUrl("/ws/market", token));
    this.socket = socket;
    socket.onopen = () => {
      if (mine === this.generation) this.subscribeAll();
    };
    socket.onmessage = (event) => {
      if (mine === this.generation) this.receive(event.data);
    };
    socket.onclose = (event) => {
      if (mine !== this.generation) return;
      this.generation += 1;
      this.detach();
      if (this.stopped) return;
      if (event.code === UNAUTHORIZED) {
        clearToken();
        return;
      }
      const wait = QUOTE_BACKOFF_SECONDS[Math.min(this.attempt, QUOTE_BACKOFF_SECONDS.length - 1)]!;
      this.attempt += 1;
      this.retry = setTimeout(() => {
        this.retry = null;
        if (!this.stopped) this.open();
      }, wait * 1000);
    };
    socket.onerror = () => undefined; // the close event follows
  }

  private subscribeAll(): void {
    this.subs.clear();
    for (const symbol of this.options.symbols) {
      this.counter += 1;
      const sub = "q" + this.counter;
      this.subs.set(sub, symbol);
      try {
        this.socket?.send(JSON.stringify({ op: "subscribe", sub, symbol, tf: "1D" }));
      } catch {
        /* the close event handles a broken socket */
      }
    }
  }

  private receive(raw: unknown): void {
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(String(raw)) as Record<string, unknown>;
    } catch {
      return;
    }
    const epochChanged = typeof message.epoch === "string" && this.epoch !== null && message.epoch !== this.epoch;
    if (typeof message.epoch === "string") this.epoch = message.epoch;
    if (message.type === "connection_status" || message.type === "pong") {
      if (epochChanged) this.subscribeAll(); // the engine restarted: its subscriptions are gone
      return;
    }
    const symbol = typeof message.sub === "string" ? this.subs.get(message.sub) : undefined;
    if (!symbol) return;
    if (message.type === "error") {
      this.options.onFailed(symbol);
    } else if (message.type === "snapshot") {
      this.attempt = 0;
      this.options.onQuote(symbol, { last: num(message.last), previousClose: num(message.previous_close), changePct: num(message.change_pct), ...origin(message) });
    } else if (message.type === "last" || message.type === "bar") {
      this.options.onQuote(symbol, { last: num(message.price), previousClose: null, changePct: num(message.change_pct), ...origin(message) });
    }
  }
}
