import { openSocket } from "@/api";
import type { Socket } from "@/api";
import type { ServerEvent } from "./types";

export type HubStatus = "connecting" | "open" | "closed";

/** One consumer of the socket: a chat tab. `id` is null until the server creates its conversation. */
export interface HubClient {
  id: string | null;
  /** The `client_ref` this tab sends with the message that creates its conversation (its draft id). */
  ref?: string | null;
  /** True while the tab waits for the server to create a conversation for it. */
  awaiting: boolean;
  onEvent: (event: ServerEvent) => void;
  /** Called every time the socket opens again, so the tab can ask for its history. */
  onOpen: () => void;
  claim: (conversationId: string) => void;
}

type Watcher = (event: ServerEvent) => void;

/** A single /ws/chat connection shared by every chat tab and by the conversation list. */
export class ChatHub {
  status: HubStatus = "connecting";
  private socket: Socket | null = null;
  private readonly clients = new Set<HubClient>();
  private readonly watchers = new Set<Watcher>();
  private readonly statusListeners = new Set<(status: HubStatus) => void>();

  constructor(private readonly connect: typeof openSocket = openSocket) {}

  private ensure(): void {
    if (this.socket) return;
    this.socket = this.connect("/ws/chat", {
      reconnect: true,
      onOpen: () => {
        this.setStatus("open");
        this.clients.forEach((client) => client.onOpen());
      },
      onClose: () => this.setStatus("closed"),
      onMessage: (data) => this.route(data),
    });
  }

  private setStatus(status: HubStatus): void {
    this.status = status;
    this.statusListeners.forEach((listener) => listener(status));
  }

  onStatus(listener: (status: HubStatus) => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /** Sees every event (the conversation list refreshes from it). */
  watch(watcher: Watcher): () => void {
    this.ensure();
    this.watchers.add(watcher);
    return () => this.watchers.delete(watcher);
  }

  register(client: HubClient): () => void {
    this.clients.add(client);
    this.ensure();
    if (this.status === "open") client.onOpen();
    return () => this.clients.delete(client);
  }

  send(message: Record<string, unknown>): boolean {
    this.ensure();
    return this.socket?.send(message) ?? false;
  }

  close(): void {
    this.socket?.close();
    this.socket = null;
    this.setStatus("closed");
  }

  private route(data: unknown): void {
    if (!data || typeof data !== "object" || typeof (data as { type?: unknown }).type !== "string") return;
    const event = data as ServerEvent;
    this.watchers.forEach((watcher) => watcher(event));
    const ref = "client_ref" in event && typeof event.client_ref === "string" ? event.client_ref : undefined;
    const cid = "conversation_id" in event ? event.conversation_id : undefined;
    if (ref) {
      // The answer to the message that created a conversation goes back to the tab that asked, whatever else is waiting.
      const asker = [...this.clients].find((client) => client.ref === ref);
      if (asker) {
        if (event.type === "conversation" && cid && asker.id === null) asker.claim(cid);
        asker.onEvent(event);
      }
      return;
    }
    if (cid) {
      const owners = [...this.clients].filter((client) => client.id === cid);
      if (owners.length > 0) {
        owners.forEach((client) => client.onEvent(event));
        return;
      }
      if (event.type === "conversation") {
        const waiting = [...this.clients].find((client) => client.id === null && client.awaiting);
        if (waiting) {
          waiting.claim(cid);
          waiting.onEvent(event);
        }
      }
      return;
    }
    [...this.clients].find((client) => client.awaiting)?.onEvent(event);
  }
}

let shared: ChatHub | null = null;

export function getHub(): ChatHub {
  if (!shared) shared = new ChatHub();
  return shared;
}

/** Tests: replace or drop the shared hub. */
export function setHub(hub: ChatHub | null): void {
  shared?.close();
  shared = hub;
}
