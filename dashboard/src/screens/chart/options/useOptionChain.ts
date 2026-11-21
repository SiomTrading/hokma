import { useCallback, useEffect, useRef, useState } from "react";
import { optionsApi, optionsCode } from "./optionsApi";
import { AUTO_OPEN, MAX_PER_CALL } from "./types";
import type { ChainResponse, ExpirationGroup, ExpirationItem, PriceFeed, UnderlyingBar } from "./types";

export interface GroupState {
  status: "loading" | "ready" | "error";
  data?: ExpirationGroup;
  error?: string;
}

export interface ChainMeta {
  price_feed: PriceFeed;
  market_open: boolean;
  as_of: string;
}

export interface OptionChainState {
  underlying: UnderlyingBar | null;
  /** The expirations list: loading, ready, or the code of what went wrong (`no_connection`, `auth_failed`...). */
  expirations: { status: "loading" | "ready" | "error"; items: ExpirationItem[]; error?: string };
  open: ReadonlySet<string>;
  groups: Readonly<Record<string, GroupState>>;
  meta: ChainMeta | null;
}

const EMPTY: OptionChainState = { underlying: null, expirations: { status: "loading", items: [] }, open: new Set(), groups: {}, meta: null };

function chunks<T>(list: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

/**
 * Loads the chain of one symbol: the bar of the underlying, the expirations, and the groups the student opens. The
 * first three groups open by themselves and load in one request; a folded group loads when it is opened; the strikes
 * and Refresh ask again for the open ones. Nothing refreshes by itself.
 */
export function useOptionChain(symbol: string, strikes: string) {
  const [state, setState] = useState<OptionChainState>(EMPTY);
  const stateRef = useRef(state);
  stateRef.current = state;
  const strikesRef = useRef(strikes);
  strikesRef.current = strikes;
  /** Each load of the whole view gets a number: an answer from an older one is dropped. */
  const epoch = useRef(0);
  const controllers = useRef(new Set<AbortController>());

  const update = useCallback((fn: (s: OptionChainState) => OptionChainState) => {
    setState((s) => {
      const next = fn(s);
      stateRef.current = next;
      return next;
    });
  }, []);

  const loadGroups = useCallback(
    (dates: readonly string[], refresh: boolean, mine: number) => {
      if (dates.length === 0) return;
      update((s) => ({
        ...s,
        groups: { ...s.groups, ...Object.fromEntries(dates.map((d) => [d, { status: "loading", data: s.groups[d]?.data } as GroupState])) },
      }));
      for (const part of chunks(dates, MAX_PER_CALL)) {
        const controller = new AbortController();
        controllers.current.add(controller);
        optionsApi
          .chain(symbol, part, strikesRef.current, refresh, controller.signal)
          .then((answer: ChainResponse) => {
            if (mine !== epoch.current) return;
            update((s) => ({
              ...s,
              groups: {
                ...s.groups,
                ...Object.fromEntries(
                  part.map((d) => {
                    const data = answer.expirations.find((g) => g.date === d);
                    return [d, data ? ({ status: "ready", data } as GroupState) : ({ status: "error", error: "upstream" } as GroupState)];
                  }),
                ),
              },
              meta: { price_feed: answer.price_feed, market_open: answer.market_open, as_of: answer.as_of },
            }));
          })
          .catch((cause: unknown) => {
            if (controller.signal.aborted || mine !== epoch.current) return;
            const error = optionsCode(cause);
            update((s) => ({
              ...s,
              groups: { ...s.groups, ...Object.fromEntries(part.map((d) => [d, { status: "error", error } as GroupState])) },
            }));
          })
          .finally(() => controllers.current.delete(controller));
      }
    },
    [symbol, update],
  );

  const loadUnderlying = useCallback(
    (mine: number) => {
      const controller = new AbortController();
      controllers.current.add(controller);
      optionsApi
        .underlying(symbol, controller.signal)
        .then((bar) => {
          if (mine === epoch.current) update((s) => ({ ...s, underlying: bar }));
        })
        .catch(() => {
          /* the bar stays as it was; the chain says what is wrong */
        })
        .finally(() => controllers.current.delete(controller));
    },
    [symbol, update],
  );

  const start = useCallback(() => {
    epoch.current += 1;
    const mine = epoch.current;
    controllers.current.forEach((c) => c.abort());
    controllers.current.clear();
    update(() => EMPTY);
    loadUnderlying(mine);
    const controller = new AbortController();
    controllers.current.add(controller);
    optionsApi
      .expirations(symbol, controller.signal)
      .then((answer) => {
        if (mine !== epoch.current) return;
        const first = answer.expirations.slice(0, AUTO_OPEN).map((item) => item.date);
        update((s) => ({ ...s, expirations: { status: "ready", items: answer.expirations }, open: new Set(first) }));
        loadGroups(first, false, mine);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted || mine !== epoch.current) return;
        update((s) => ({ ...s, expirations: { status: "error", items: [], error: optionsCode(cause) } }));
      })
      .finally(() => controllers.current.delete(controller));
  }, [symbol, update, loadUnderlying, loadGroups]);

  useEffect(() => {
    start();
    const live = controllers.current;
    return () => {
      epoch.current += 1;
      live.forEach((c) => c.abort());
      live.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a new symbol starts over; the rest are stable callbacks of it
  }, [symbol]);

  /** Open or fold a group. Folding keeps what was loaded; opening a group that is not loaded asks for it. */
  const toggle = useCallback(
    (date: string) => {
      const current = stateRef.current;
      const open = new Set(current.open);
      if (open.has(date)) {
        open.delete(date);
        update((s) => ({ ...s, open }));
        return;
      }
      open.add(date);
      update((s) => ({ ...s, open }));
      if (current.groups[date]?.status !== "ready") loadGroups([date], false, epoch.current);
    },
    [update, loadGroups],
  );

  /** Ask again for the open groups (the strikes changed, or Refresh). The bar of the underlying too on Refresh. */
  const reload = useCallback(
    (refresh: boolean) => {
      const dates = stateRef.current.expirations.items.map((item) => item.date).filter((d) => stateRef.current.open.has(d));
      if (refresh) loadUnderlying(epoch.current);
      loadGroups(dates, refresh, epoch.current);
    },
    [loadGroups, loadUnderlying],
  );

  return { state, toggle, reload, restart: start };
}
