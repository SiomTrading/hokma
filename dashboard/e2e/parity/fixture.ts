import type { ChartData } from "../../src/screens/chart/types";

/** Fixed NQ=F 5m session used by the visual parity check (T-1): the same bars for every capture. */
const START = 1793802600; // 2026-11-04 09:30 New York

function bars() {
  const out = [];
  let price = 21000;
  for (let i = 0; i < 60; i += 1) {
    const wave = Math.sin(i / 4) * 18 + Math.cos(i / 9) * 11;
    const o = price;
    const c = 21000 + wave + i * 1.5;
    const h = Math.max(o, c) + 4 + (i % 5);
    const l = Math.min(o, c) - 4 - (i % 3);
    out.push({ t: START + i * 300, o, h, l, c, v: 800 + i * 13 });
    price = c;
  }
  return out;
}

export const candles = bars();

export const data: ChartData = {
  symbol: "NQ=F",
  name: "Nasdaq 100 Dec 26",
  kind: "future",
  exchange: "CME",
  currency: "USD",
  timezone: "America/New_York",
  timeframe: "5m",
  candles,
  last_price: candles[candles.length - 1]!.c,
  previous_close: 21000,
  change: 10,
  change_pct: 0.05,
  market_open: true,
  last_trade_at: candles[candles.length - 1]!.t,
  data_age_seconds: 0,
  delayed: false,
  opening_range: { high: Math.max(...candles.slice(0, 6).map((c) => c.h)), session_date: "2026-11-04", start: START, end: START + 1800, complete: true },
  source: "Coinbase",
  stale: false,
  fetched_at: candles[candles.length - 1]!.t,
};
