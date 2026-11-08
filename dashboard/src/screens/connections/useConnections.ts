import { useCallback, useEffect, useRef, useState } from "react";
import { openSocket } from "@/api";
import type { Socket } from "@/api";
import { connectionErrorText, connectionsApi } from "./api";
import type { ConnectionCard, ConnectionStatusMessage, ConnectionsList } from "./types";

const PING_MS = 15_000;

export interface ConnectionsState {
  cards: ConnectionCard[];
  groups: ConnectionsList["groups"];
  storeAvailable: boolean;
  loading: boolean;
  error: string | null;
  reload: () => void;
  patchCard: (card: ConnectionCard) => void;
  setStoreAvailable: (available: boolean) => void;
  setGroups: (groups: ConnectionsList["groups"]) => void;
}

/** Merges the live part of a `connection_status` message into the card it names. */
export function applyStatus(cards: ConnectionCard[], message: ConnectionStatusMessage): ConnectionCard[] {
  return cards.map((card) =>
    card.id === message.provider
      ? {
          ...card,
          state: message.state,
          data_state: message.data_state,
          error_code: message.error_code,
          enabled: message.enabled,
          key_state: message.key_state,
          symbols_in_use: message.symbols_in_use,
        }
      : card,
  );
}

function isStatus(data: unknown): data is ConnectionStatusMessage {
  return !!data && typeof data === "object" && (data as { type?: unknown }).type === "connection_status" && typeof (data as { provider?: unknown }).provider === "string";
}

/**
 * The cards, kept current: one load, then the `connection_status` messages of `/ws/market` (no subscription needed).
 * When the socket opens again the list is loaded once more, so nothing that happened while it was down is missed.
 */
export function useConnections(): ConnectionsState {
  const [cards, setCards] = useState<ConnectionCard[]>([]);
  const [groups, setGroups] = useState<ConnectionsList["groups"]>({});
  const [storeAvailable, setStoreAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const ticket = useRef(0);

  const load = useCallback(() => {
    const mine = (ticket.current += 1);
    connectionsApi.list().then(
      (data) => {
        if (mine !== ticket.current) return;
        setCards(data.connections);
        setGroups(data.groups);
        setStoreAvailable(data.store_available);
        setError(null);
        setLoading(false);
      },
      (cause: unknown) => {
        if (mine !== ticket.current) return;
        setError(connectionErrorText(cause));
        setLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    load();
    let socket: Socket | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;
    let opened = false;
    socket = openSocket("/ws/market", {
      reconnect: true,
      onOpen: () => {
        if (opened) load();
        opened = true;
        if (timer) clearInterval(timer);
        timer = setInterval(() => socket?.send({ op: "ping" }), PING_MS);
      },
      onClose: () => {
        if (timer) clearInterval(timer);
        timer = null;
      },
      onMessage: (data) => {
        if (isStatus(data)) setCards((current) => applyStatus(current, data));
      },
    });
    return () => {
      ticket.current += 1;
      if (timer) clearInterval(timer);
      socket?.close();
    };
  }, [load]);

  const patchCard = useCallback((card: ConnectionCard) => setCards((current) => current.map((c) => (c.id === card.id ? card : c))), []);
  return { cards, groups, storeAvailable, loading, error, reload: load, patchCard, setStoreAvailable, setGroups };
}
