import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "@/i18n";
import { formatMoment } from "./format";
import { IMAGE_HEIGHT, IMAGE_WIDTH, fitPlot, renderChartPng } from "./exportPng";
import type { PriceChartHandle } from "./PriceChart";
import type { ChartData } from "./types";

const START = 1793802600;

const data: ChartData = {
  symbol: "NQ=F", name: "Nasdaq 100 Dec 26", kind: "future", exchange: "CME", currency: "USD", timezone: "America/New_York", timeframe: "5m",
  candles: Array.from({ length: 12 }, (_, i) => ({ t: START + i * 300, o: 100 + i, h: 103 + i, l: 98 + i, c: i % 2 ? 99 + i : 102 + i, v: 5 })),
  last_price: 110, previous_close: 100, change: 10, change_pct: 10, market_open: false, last_trade_at: START + 11 * 300, data_age_seconds: null,
  delayed: false, opening_range: { high: 108, session_date: "2026-11-04", start: START, end: START + 1800, complete: true },
  source: "Yahoo Finance", stale: false, fetched_at: START + 11 * 300,
};

/** The picture the chart library hands over: an 800 x 400 canvas. */
const shot = { width: 800, height: 400 } as HTMLCanvasElement;
function handle(overrides: Partial<PriceChartHandle> = {}): PriceChartHandle {
  return { takeScreenshot: () => shot, priceToCoordinate: () => 120, priceAxisWidth: () => 80, ...overrides };
}

function fakeCanvas() {
  const calls = { text: [] as Array<{ text: string; x: number; y: number }>, images: [] as Array<{ image: unknown; x: number; y: number; w: number; h: number }>, rounds: 0 };
  const ctx = {
    fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "left", textBaseline: "alphabetic",
    fillRect: () => undefined,
    fillText: (text: string, x: number, y: number) => { calls.text.push({ text, x, y }); },
    measureText: (text: string) => ({ width: text.length * 8 }),
    drawImage: (image: unknown, x: number, y: number, w: number, h: number) => { calls.images.push({ image, x, y, w, h }); },
    beginPath: () => undefined, moveTo: () => undefined, lineTo: () => undefined, stroke: () => undefined, fill: () => undefined, rect: () => undefined,
    roundRect: () => { calls.rounds += 1; },
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

describe("renderChartPng (T-14)", () => {
  it("is 1280 x 720 with the header, the chart picture, the opening pill and the footer", async () => {
    const { canvas, calls } = fakeCanvas();
    const blob = await renderChartPng(data, { root: document.documentElement, t, locale: "en", chart: handle() });
    expect(blob.type).toBe("image/png");
    expect(canvas.width).toBe(IMAGE_WIDTH);
    expect(canvas.height).toBe(IMAGE_HEIGHT);
    const texts = calls.text.map((c) => c.text);
    // header: symbol and interval, name, last price and change
    expect(texts).toContain("NQ=F · 5m");
    expect(texts).toContain(t("chart.image.sub", { name: "Nasdaq 100 Dec 26", exchange: "CME" }));
    expect(texts).toContain("110.00");
    expect(texts).toContain("+10.00%");
    // the chart is the library's own picture, not a redrawing
    expect(calls.images).toHaveLength(1);
    expect(calls.images[0]!.image).toBe(shot);
    expect(calls.images[0]!.y).toBeGreaterThanOrEqual(96);
    expect(calls.images[0]!.y + calls.images[0]!.h).toBeLessThanOrEqual(IMAGE_HEIGHT - 34);
    // the pill on top of it
    expect(texts).toContain(`${t("chart.opening")} 108.00`);
    expect(calls.rounds).toBe(1);
    // footer: source and time, at the bottom
    const footer = calls.text.at(-1)!;
    expect(footer.text).toBe(t("chart.image.updated", { source: "Yahoo Finance", when: formatMoment(START + 11 * 300, "America/New_York", "en") }));
    expect(footer.y).toBe(IMAGE_HEIGHT - 12);
  });

  it("leaves the pill out when the opening line is not on the chart", async () => {
    const { calls } = fakeCanvas();
    await renderChartPng(data, { root: document.documentElement, t, locale: "en", chart: handle({ priceToCoordinate: () => null }) });
    expect(calls.text.map((c) => c.text)).not.toContain(`${t("chart.opening")} 108.00`);
    await renderChartPng({ ...data, opening_range: { ...data.opening_range!, start: START - 3600 } }, { root: document.documentElement, t, locale: "en", chart: handle() });
    expect(calls.text.filter((c) => c.text === `${t("chart.opening")} 108.00`)).toHaveLength(0);
  });

  it("fails clearly when the browser cannot draw", async () => {
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => (tag === "canvas" ? { getContext: () => null } : Document.prototype.createElement.call(document, tag))) as typeof document.createElement);
    await expect(renderChartPng(data, { root: document.documentElement, t, locale: "en", chart: handle() })).rejects.toThrow();
  });

  it("fails when the chart cannot give its picture", async () => {
    fakeCanvas();
    const broken = handle({ takeScreenshot: () => { throw new Error("chart not ready"); } });
    await expect(renderChartPng(data, { root: document.documentElement, t, locale: "en", chart: broken })).rejects.toThrow("chart not ready");
  });

  it("fails when the PNG cannot be encoded", async () => {
    const { canvas } = fakeCanvas();
    canvas.toBlob = (done: (blob: Blob | null) => void) => done(null);
    await expect(renderChartPng(data, { root: document.documentElement, t, locale: "en", chart: handle() })).rejects.toThrow();
  });
});

describe("fitPlot", () => {
  it("fits the picture inside the plot area without stretching it", () => {
    const box = { x: 0, y: 97, width: 1280, height: 589 };
    const wide = fitPlot({ width: 2560, height: 800 }, box);
    expect(wide.width).toBe(1280);
    expect(wide.height).toBeCloseTo(400, 6);
    expect(wide.y).toBeCloseTo(97 + (589 - 400) / 2, 6);
    const tall = fitPlot({ width: 400, height: 589 }, box);
    expect(tall.height).toBeCloseTo(589, 6);
    expect(tall.x).toBeCloseTo((1280 - 400) / 2, 6);
    expect(fitPlot({ width: 0, height: 0 }, box).width).toBe(0);
  });
});
