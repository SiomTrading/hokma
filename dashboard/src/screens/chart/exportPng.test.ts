import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { IMAGE_HEIGHT, IMAGE_WIDTH, renderChartPng } from "./exportPng";
import type { ChartData } from "./types";

const START = 1793802600;

const data: ChartData = {
  symbol: "NQ=F", name: "Nasdaq 100 Dec 26", kind: "future", exchange: "CME", currency: "USD", timezone: "America/New_York", timeframe: "5m",
  candles: Array.from({ length: 12 }, (_, i) => ({ t: START + i * 300, o: 100 + i, h: 103 + i, l: 98 + i, c: i % 2 ? 99 + i : 102 + i, v: 5 })),
  last_price: 110, previous_close: 100, change: 10, change_pct: 10, market_open: false, last_trade_at: START + 11 * 300, data_age_seconds: null,
  delayed: false, opening_range: { high: 108, session_date: "2026-11-04", start: START, end: START + 1800, complete: true },
  source: "Yahoo Finance", stale: false, fetched_at: START + 11 * 300,
};

function fakeCanvas() {
  const calls = { text: [] as string[], fills: 0, dashes: [] as number[][], strokes: 0 };
  const ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "left", textBaseline: "alphabetic",
    fillRect: () => { calls.fills += 1; },
    fillText: (text: string) => { calls.text.push(text); },
    beginPath: () => undefined, moveTo: () => undefined, lineTo: () => undefined,
    stroke: () => { calls.strokes += 1; },
    setLineDash: (pattern: number[]) => { calls.dashes.push(pattern); },
  };
  const canvas = {
    width: 0, height: 0,
    getContext: () => ctx,
    toBlob: (done: (blob: Blob | null) => void) => done(new Blob(["png"], { type: "image/png" })),
  };
  vi.spyOn(document, "createElement").mockImplementation(((tag: string) => (tag === "canvas" ? canvas : Document.prototype.createElement.call(document, tag))) as typeof document.createElement);
  return { canvas, calls };
}

afterEach(() => vi.restoreAllMocks());

describe("renderChartPng", () => {
  it("draws the symbol, last price, candles, opening line and its label into a PNG", async () => {
    const { canvas, calls } = fakeCanvas();
    const blob = await renderChartPng(data, { root: document.documentElement, t, locale: "en" });
    expect(blob.type).toBe("image/png");
    expect(canvas.width).toBe(IMAGE_WIDTH);
    expect(canvas.height).toBe(IMAGE_HEIGHT);
    expect(calls.text).toContain("NQ=F · 5m");
    expect(calls.text).toContain("110.00");
    expect(calls.text).toContain("+10.00%");
    expect(calls.text).toContain(`${t("chart.opening")} 108.00`);
    expect(calls.dashes).toContainEqual([8, 8]); // the dashed line
    expect(calls.fills).toBeGreaterThanOrEqual(1 + data.candles.length); // background plus one body per candle
  });

  it("fails clearly when the browser cannot draw", async () => {
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => (tag === "canvas" ? { getContext: () => null } : Document.prototype.createElement.call(document, tag))) as typeof document.createElement);
    await expect(renderChartPng(data, { root: document.documentElement, t, locale: "en" })).rejects.toThrow();
  });

  it("fails when the PNG cannot be encoded", async () => {
    const { canvas } = fakeCanvas();
    canvas.toBlob = (done: (blob: Blob | null) => void) => done(null);
    await expect(renderChartPng(data, { root: document.documentElement, t, locale: "en" })).rejects.toThrow();
  });
});
