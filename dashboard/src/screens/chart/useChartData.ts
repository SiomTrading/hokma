import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import type { ChartData, Timeframe } from "./types";

/** Seconds between refreshes while the market is open and the tab is in front. */
export const REFRESH_SECONDS: Record<Timeframe, number> = { "1m": 15, "5m": 30, "15m": 60, "1h": 120, "1D": 300 };

export interface ChartState {
  data: ChartData | null;
  loading: boolean;
  /** Message of the last failed request (first load or refresh). */
  error: string | null;
  /** True when the failure happened while there was already data on screen. */
  refreshFailed: boolean;
  reload: () => void;
}

/** Loads `/api/market/candles` for one symbol and timeframe, ignores answers that arrive late and keeps the chart fresh. */
export function useChartData(symbol: string, timeframe: Timeframe, active: boolean): ChartState {
  const [data, setData] = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const latest = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const shown = useRef<ChartData | null>(null);

  const load = useCallback(
    (background: boolean) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      const ticket = (latest.current += 1);
      if (!background) setLoading(true);
      api
        .get<ChartData>(`/api/market/candles?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(timeframe)}`, { signal: abort.signal })
        .then((next) => {
          if (ticket !== latest.current) return;
          shown.current = next;
          setData(next);
          setError(null);
          setRefreshFailed(false);
        })
        .catch((failure: unknown) => {
          if (ticket !== latest.current || abort.signal.aborted) return;
          const message = failure instanceof Error ? failure.message : "";
          setError(message);
          setRefreshFailed(shown.current !== null && shown.current.symbol === symbol && shown.current.timeframe === timeframe);
        })
        .finally(() => {
          if (ticket === latest.current) setLoading(false);
        });
    },
    [symbol, timeframe],
  );

  // New symbol or timeframe: drop what is on screen and load. A refresh of the same chart keeps it.
  useEffect(() => {
    shown.current = null;
    setData(null);
    setError(null);
    setRefreshFailed(false);
    load(false);
    return () => {
      latest.current += 1;
      controller.current?.abort();
    };
  }, [load]);

  const marketOpen = data?.market_open ?? false;
  useEffect(() => {
    if (!active || !marketOpen) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") load(true);
    }, REFRESH_SECONDS[timeframe] * 1000);
    return () => clearInterval(timer);
  }, [active, marketOpen, timeframe, load]);

  const reload = useCallback(() => load(shown.current !== null), [load]);
  // Right after a timeframe change the old candles are still in state for one render: never show them under the new title.
  const current = data !== null && data.timeframe === timeframe ? data : null;
  return { data: current, loading: loading || (current === null && error === null), error, refreshFailed, reload };
}
