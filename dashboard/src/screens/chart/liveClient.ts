import { clearToken, getToken, socketUrl } from "@/api";
import { applyBar, applyLast, applySnapshot, applyStatus, errorOf, initialState } from "./liveState";
import type { LiveState, SubMessage } from "./liveState";
import type { Timeframe } from "./types";

/** Seconds to wait before each new attempt; the last one repeats. */
export const BACKOFF_SECONDS = [1, 2, 4, 8, 16, 30] as const;
/** Nothing heard for this long (not even an answer to a ping): the connection is dead. */
export const DEAD_AFTER_MS = 45_000;
export const PING_EVERY_MS = 15_000;
const UNAUTHORIZED = 4401;

/** The part of `WebSocket` the client uses; tests hand in a stand-in. */
export interface SocketLike {
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  send(data: string): void;
  close(code?: number): void;
}

export interface LiveClientOptions {
  symbol: string;
  /** Canonical interval sent as `tf`: time (`7m`, `15s`, `1D`) or range (`r:10t`, `r:2.5p`). */
  timeframe: Timeframe;
  onState: (state: LiveState) => void;
  createSocket?: (url: string) => SocketLike;
}

/**
 * The chart's only source of data: one subscription on `/ws/market`. It never asks the HTTP API for candles.
 *
 * Rules (SPEC-001 addendum 5.3):
 *  - nothing heard for 45 s: the connection is dead, a new one is opened after 1, 2, 4 ... 30 s;
 *  - every connection subscribes again and waits for a new `snapshot`;
 *  - another `epoch` (the engine restarted) drops every earlier `seq`;
 *  - a message whose `seq` is not newer than the last one is ignored; a jump in `seq` asks for a new snapshot.
 *
 * A new snapshot is a new subscription with a new id: the engine refuses a second `subscribe` with the same id, and
 * the old id stops being listened to, so late messages of the old one can never mix in.
 */
export class LiveChartClient {
  private state: LiveState;
  private socket: SocketLike | null = null;
  private stopped = true;
  private generation = 0;
  private subNumber = 0;
  private sub = "";
  private epoch: string | null = null;
  private lastSeq = 0;
  private attempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private deadTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly options: LiveClientOptions) {
    this.state = initialState(options.symbol, options.timeframe);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.generation += 1;
    this.clearTimers();
    this.detach();
  }

  /** The owner pressed "Try again" after an error: ask for the symbol once more. */
  retry(): void {
    this.update({ ...this.state, error: null });
    if (this.socket) this.resubscribe();
  }

  private update(next: LiveState): void {
    this.state = next;
    this.options.onState(next);
  }

  private clearTimers(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.deadTimer) clearTimeout(this.deadTimer);
    this.retryTimer = this.pingTimer = this.deadTimer = null;
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
      if (mine !== this.generation) return;
      this.update({ ...this.state, link: true });
      this.hear();
      this.pingTimer = setInterval(() => this.send({ op: "ping" }), PING_EVERY_MS);
      this.subscribe();
    };
    socket.onmessage = (event) => {
      if (mine !== this.generation) return;
      this.hear();
      this.receive(event.data);
    };
    socket.onclose = (event) => {
      if (mine !== this.generation) return;
      this.lose(event.code);
    };
    socket.onerror = () => undefined; // the close event follows and carries the handling
  }

  /** Something arrived: the connection is alive, restart the 45 s clock. */
  private hear(): void {
    if (this.deadTimer) clearTimeout(this.deadTimer);
    this.deadTimer = setTimeout(() => this.lose(0), DEAD_AFTER_MS);
  }

  /** The connection is gone or dead: drop it and try again after the wait. */
  private lose(code: number): void {
    this.generation += 1;
    this.clearTimers();
    this.detach();
    if (this.stopped) return;
    if (code === UNAUTHORIZED) {
      clearToken();
      return;
    }
    this.update({ ...this.state, link: false });
    const wait = BACKOFF_SECONDS[Math.min(this.attempt, BACKOFF_SECONDS.length - 1)]!;
    this.attempt += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.stopped) this.open();
    }, wait * 1000);
  }

  private send(message: Record<string, unknown>): void {
    try {
      this.socket?.send(JSON.stringify(message));
    } catch {
      /* the close event handles a broken socket */
    }
  }

  private subscribe(): void {
    this.subNumber += 1;
    this.sub = "c" + this.subNumber;
    this.lastSeq = 0;
    this.send({ op: "subscribe", sub: this.sub, symbol: this.options.symbol, tf: this.options.timeframe });
  }

  /** Ask for a fresh snapshot: leave the current subscription and open a new one. */
  private resubscribe(): void {
    if (this.sub) this.send({ op: "unsubscribe", sub: this.sub });
    this.subscribe();
  }

  private receive(raw: unknown): void {
    let message: SubMessage;
    try {
      message = JSON.parse(String(raw)) as SubMessage;
    } catch {
      return;
    }
    if (!message || typeof message !== "object" || typeof message.type !== "string") return;

    const epochChanged = typeof message.epoch === "string" && this.epoch !== null && message.epoch !== this.epoch;
    if (typeof message.epoch === "string") this.epoch = message.epoch;
    if (epochChanged) this.lastSeq = 0; // the engine restarted: every earlier seq means nothing now

    if (message.type === "pong" || message.type === "connection_status") {
      if (epochChanged) this.resubscribe(); // its subscriptions are gone with the old run
      return;
    }
    if (message.sub !== this.sub) return; // another subscription, or an old one

    if (message.type === "error") {
      if (typeof message.seq === "number" && message.seq > this.lastSeq) this.lastSeq = message.seq;
      this.update({ ...this.state, error: errorOf(message) });
      return;
    }
    if (message.type === "snapshot") {
      if (!epochChanged && typeof message.seq === "number" && message.seq <= this.lastSeq) return;
      this.lastSeq = typeof message.seq === "number" ? message.seq : 0;
      this.attempt = 0;
      this.update(applySnapshot(this.state, message));
      return;
    }
    if (epochChanged) {
      this.resubscribe(); // a message of the new run before any snapshot of it
      return;
    }
    const seq = message.seq;
    if (typeof seq !== "number") return;
    if (seq <= this.lastSeq) return;
    if (seq > this.lastSeq + 1) {
      this.resubscribe(); // something was lost: start again from a snapshot
      return;
    }
    this.lastSeq = seq;
    if (message.type === "bar") this.update(applyBar(this.state, message));
    else if (message.type === "last") this.update(applyLast(this.state, message));
    else if (message.type === "status") this.update(applyStatus(this.state, message));
  }
}
