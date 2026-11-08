import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { canonicalInterval } from "./intervals";
import { LiveChartClient } from "./liveClient";
import type { LiveClientOptions } from "./liveClient";
import { initialState, toChartData } from "./liveState";
import type { LiveState } from "./liveState";
import type { ChartData, Timeframe } from "./types";

export interface LiveChart {
  live: LiveState;
  /** The same data in the shape the drawing and the PNG export use; null until the first snapshot. */
  data: ChartData | null;
  retry: () => void;
}

/**
 * The chart's data: one subscription on `/ws/market` for one symbol and interval (time or range, written in any form
 * of the grammar; it is sent in its canonical form). Nothing here asks the HTTP API for candles, ever. A new symbol or
 * interval opens a new subscription and drops the old data at once, so candles of one chart never show under the
 * title of another.
 */
export function useLiveChart(symbol: string, timeframe: Timeframe, createSocket?: LiveClientOptions["createSocket"]): LiveChart {
  const tf = canonicalInterval(timeframe) ?? timeframe; // an invalid one reaches the engine, which answers market.bad_request
  const [live, setLive] = useState<LiveState>(() => initialState(symbol, tf));
  const client = useRef<LiveChartClient | null>(null);

  useEffect(() => {
    setLive(initialState(symbol, tf));
    const next = new LiveChartClient({ symbol, timeframe: tf, onState: setLive, createSocket });
    client.current = next;
    next.start();
    return () => {
      next.stop();
      if (client.current === next) client.current = null;
    };
  }, [symbol, tf, createSocket]);

  const retry = useCallback(() => client.current?.retry(), []);
  // Right after a change the old state is still there for one render: never show it under the new title.
  const current = live.symbol === symbol && live.timeframe === tf ? live : initialState(symbol, tf);
  const data = useMemo(() => (current.ready ? toChartData(current) : null), [current]);
  return { live: current, data, retry };
}
