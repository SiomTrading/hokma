import { act } from "@testing-library/react";
import type { SocketHandlers } from "@/api";
import { ChatHub, setHub } from "./hub";

/** A stand-in for the /ws/chat connection: records what the panel sends and lets a test push server events. */
export class FakeServer {
  sent: Array<Record<string, unknown>> = [];
  handlers: SocketHandlers = {};
  connected = false;
  hub: ChatHub;

  constructor() {
    this.hub = new ChatHub((_path, handlers = {}) => {
      this.handlers = handlers;
      return {
        send: (data) => {
          if (!this.connected) return false;
          this.sent.push(data as Record<string, unknown>);
          return true;
        },
        close: () => undefined,
      };
    });
    setHub(this.hub);
  }

  open(): void {
    this.connected = true;
    act(() => this.handlers.onOpen?.());
  }

  drop(): void {
    this.connected = false;
    act(() => this.handlers.onClose?.({ code: 1006 } as CloseEvent));
  }

  emit(event: Record<string, unknown>): void {
    act(() => this.handlers.onMessage?.(event));
  }

  last(type: string): Record<string, unknown> | undefined {
    return [...this.sent].reverse().find((m) => m.type === type);
  }
}
