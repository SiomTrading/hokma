import { clearToken, getToken, socketUrl } from "@/api";
import type { SocketLike } from "../liveClient";
import type { ExpirationGroup, OptionCell } from "./types";

/** Seconds to wait before each new attempt of the live chain; the last one repeats. */
export const LIVE_BACKOFF_SECONDS = [1, 2, 4, 8, 16, 30] as const;
const UNAUTHORIZED = 4401;

/** What a message of one contract changes (SPEC-016 4.3): the fields Schwab sent and the ones Hokma calculates from them. */
export interface CellChange extends Partial<OptionCell> {
  symbol: string;
}

export interface LiveOptionsHandlers {
  /** The visible rows, chosen by the engine around the price of the stream: they replace what the groups held. */
  onSnapshot: (groups: ExpirationGroup[], price: number | null) => void;
  onChanges: (changes: CellChange[], price: number | null) => void;
  onLimit: (symbols: string[], open: number) => void;
  /** `live`, or `stopped` when the source lost its connection (what is on screen stays, grey). */
  onLink: (state: "live" | "stopped") => void;
  /** The source of the user has no streaming options (a free source): the chain stays a photo that Refresh renews. */
  onNotLive: () => void;
}

export interface LiveOptionsOptions extends LiveOptionsHandlers {
  symbol: string;
  expirations: readonly string[];
  /** A number of strikes ("14") or "all". */
  strikes: string;
  createSocket?: (url: string) => SocketLike;
}

interface Wire {
  type?: string;
  sub?: string;
  seq?: number;
  epoch?: string;
  code?: string;
  state?: string;
  underlying_price?: number | null;
  expirations?: ExpirationGroup[];
  changes?: CellChange[];
  symbols?: string[];
  open?: number;
}

/** The option chain that moves by itself: one `options` subscription of `/ws/market`. It never asks the HTTP API for prices. */
export class LiveOptionsClient {
  private socket: SocketLike | null = null;
  private stopped = true;
  private generation = 0;
  private sub = "";
  private subNumber = 0;
  private epoch: string | null = null;
  private lastSeq = 0;
  private attempt = 0;
  private retry: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: LiveOptionsOptions) {}

  start(): void {
    if (!this.stopped) return;
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
      if (mine === this.generation) this.subscribe();
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
      this.options.onLink("stopped");
      const wait = LIVE_BACKOFF_SECONDS[Math.min(this.attempt, LIVE_BACKOFF_SECONDS.length - 1)]!;
      this.attempt += 1;
      this.retry = setTimeout(() => {
        this.retry = null;
        if (!this.stopped) this.open();
      }, wait * 1000);
    };
    socket.onerror = () => undefined; // the close event follows
  }

  private subscribe(): void {
    this.subNumber += 1;
    this.sub = "o" + this.subNumber;
    this.lastSeq = 0;
    const { symbol, expirations, strikes } = this.options;
    const count = strikes === "all" ? "all" : Number(strikes);
    try {
      this.socket?.send(JSON.stringify({ op: "options", sub: this.sub, symbol, expirations, strikes: count }));
    } catch {
      /* the close event handles a broken socket */
    }
  }

  private receive(raw: unknown): void {
    let message: Wire;
    try {
      message = JSON.parse(String(raw)) as Wire;
    } catch {
      return;
    }
    if (!message || typeof message.type !== "string") return;
    const epochChanged = typeof message.epoch === "string" && this.epoch !== null && message.epoch !== this.epoch;
    if (typeof message.epoch === "string") this.epoch = message.epoch;
    if (epochChanged) this.lastSeq = 0; // the engine restarted: its subscriptions are gone
    if (message.type === "connection_status" || message.type === "pong") {
      if (epochChanged) this.subscribe();
      return;
    }
    if (message.sub !== this.sub) return;
    if (message.type === "error") {
      if (message.code === "market.options_not_live") this.options.onNotLive();
      return;
    }
    if (typeof message.seq !== "number" || message.seq <= this.lastSeq) return;
    if (message.type !== "options_snapshot" && message.seq > this.lastSeq + 1) {
      this.subscribe(); // something was lost: start again from a snapshot
      return;
    }
    this.lastSeq = message.seq;
    if (message.type === "options_snapshot") {
      this.attempt = 0;
      this.options.onLink("live");
      this.options.onSnapshot(message.expirations ?? [], message.underlying_price ?? null);
    } else if (message.type === "options") {
      this.options.onChanges(message.changes ?? [], message.underlying_price ?? null);
    } else if (message.type === "options_limit") {
      this.options.onLimit(message.symbols ?? [], message.open ?? 0);
    } else if (message.type === "options_status") {
      this.options.onLink(message.state === "live" ? "live" : "stopped");
    }
  }
}

/** The groups with the changes of the contracts put in their cells, as new objects (a group that no change touches is the same object).
 *  A contract that is not in a group is ignored. */
export function applyChanges<G extends { data?: ExpirationGroup }>(groups: Readonly<Record<string, G>>, changes: readonly CellChange[]): Record<string, G> {
  const bySymbol = new Map(changes.map((change) => [change.symbol, change]));
  const out: Record<string, G> = {};
  for (const [date, group] of Object.entries(groups)) {
    const data = group.data;
    let touched = false;
    const rows = data?.rows.map((row) => {
      const merged = { ...row };
      for (const side of ["call", "put"] as const) {
        const cell = row[side];
        const change = cell ? bySymbol.get(cell.symbol) : undefined;
        if (cell && change) {
          merged[side] = { ...cell, ...change };
          touched = true;
        }
      }
      return merged;
    });
    out[date] = data && touched && rows ? { ...group, data: { ...data, rows } } : group;
  }
  return out;
}
