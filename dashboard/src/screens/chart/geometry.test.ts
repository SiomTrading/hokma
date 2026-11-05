import { describe, expect, it } from "vitest";
import { formatBarTime, formatPercent, formatPrice, safeFileName } from "./format";
import { VIEW_H, candleIndexAt, layoutChart, openingVisible } from "./geometry";
import type { Candle, OpeningRange } from "./types";

const candle = (i: number, o: number, h: number, l: number, c: number): Candle => ({ t: 1000 + i * 300, o, h, l, c, v: 10 });
const opening = (high: number, start = 1000): OpeningRange => ({ high, session_date: "2026-11-04", start, end: start + 1800, complete: true });

describe("layoutChart", () => {
  const candles = [candle(0, 10, 14, 9, 12), candle(1, 12, 13, 8, 9), candle(2, 9, 11, 9, 11)];

  it("maps higher prices to smaller y and colors by direction", () => {
    const layout = layoutChart(candles, null);
    const [first, second, third] = layout.shapes;
    expect(first!.up).toBe(true);
    expect(second!.up).toBe(false);
    expect(third!.up).toBe(true);
    expect(first!.wickTop).toBeLessThan(first!.wickBottom);
    expect(first!.bodyY).toBeGreaterThan(first!.wickTop);
    expect(first!.bodyY + first!.bodyH).toBeLessThan(first!.wickBottom + 0.001);
    // the highest high is above every other price on screen
    expect(Math.min(...layout.shapes.map((s) => s.wickTop))).toBe(first!.wickTop);
    expect(layout.openingY).toBeNull();
  });

  it("keeps everything inside the canvas and gives a doji a visible body", () => {
    const layout = layoutChart([candle(0, 10, 10, 10, 10), candle(1, 10, 12, 8, 11)], null);
    for (const shape of layout.shapes) {
      expect(shape.wickTop).toBeGreaterThanOrEqual(0);
      expect(shape.wickBottom).toBeLessThanOrEqual(VIEW_H);
    }
    expect(layout.shapes[0]!.bodyH).toBeGreaterThanOrEqual(1.5);
  });

  it("does not collapse when every price is the same", () => {
    const layout = layoutChart([candle(0, 5, 5, 5, 5)], null);
    expect(layout.max).toBeGreaterThan(layout.min);
    expect(Number.isFinite(layout.shapes[0]!.bodyY)).toBe(true);
  });

  it("places the opening line at the y of its price and widens the scale to include it", () => {
    const layout = layoutChart(candles, opening(20));
    expect(layout.openingY).not.toBeNull();
    expect(layout.max).toBeGreaterThan(20);
    expect(layout.priceAt(layout.openingY!)).toBeCloseTo(20, 6);
    expect(layout.openingY!).toBeLessThan(layout.shapes[0]!.wickTop);
  });

  it("draws the line at the high of the window when it sits among the candles", () => {
    const layout = layoutChart(candles, opening(14));
    expect(layout.openingY!).toBeCloseTo(layout.shapes[0]!.wickTop, 6);
  });

  it("hides the opening line when its session starts before the candles shown", () => {
    expect(openingVisible(candles, opening(14, 10))).toBe(false);
    expect(layoutChart(candles, opening(14, 10)).openingY).toBeNull();
  });

  it("handles no candles", () => {
    const layout = layoutChart([], null);
    expect(layout.shapes).toEqual([]);
    expect(layout.grid).toHaveLength(5);
  });

  it("finds the candle under the pointer", () => {
    expect(candleIndexAt(0, 10)).toBe(-1);
    expect(candleIndexAt(1000, 10)).toBe(-1);
    expect(candleIndexAt(20, 10)).toBe(0);
    expect(candleIndexAt(980, 10)).toBe(9);
    expect(candleIndexAt(500, 0)).toBe(-1);
  });
});

describe("format", () => {
  it("formats prices and percentages with the locale", () => {
    expect(formatPrice(21482.25, "en")).toBe("21,482.25");
    expect(formatPrice(21482.25, "es")).toBe("21.482,25");
    expect(formatPrice(0.00123, "en")).toBe("0.00123");
    expect(formatPrice(null, "en")).toBe("-");
    expect(formatPercent(0.4231, "en")).toBe("+0.42%");
    expect(formatPercent(-1.5, "en")).toBe("-1.50%");
    expect(formatPercent(0, "en")).toBe("0.00%");
  });

  it("shows bar times in the exchange time zone", () => {
    const nineThirtyNewYork = 1793802600; // 2026-11-04 09:30 EST
    expect(formatBarTime(nineThirtyNewYork, "5m", "America/New_York", "en", 3600)).toBe("09:30");
    expect(formatBarTime(nineThirtyNewYork, "5m", "UTC", "en", 3600)).toBe("14:30");
    expect(formatBarTime(nineThirtyNewYork, "15m", "America/New_York", "en", 40 * 3600)).toContain("09:30");
    expect(formatBarTime(nineThirtyNewYork, "1D", "America/New_York", "en", 0)).toContain("Nov");
    expect(formatBarTime(nineThirtyNewYork, "5m", "Not/AZone", "en", 3600)).toMatch(/\d\d:\d\d/);
  });

  it("builds a safe file name for the attachment", () => {
    expect(safeFileName("NQ=F", "5m")).toBe("chart-NQ-F-5m.png");
    expect(safeFileName("^GSPC", "1D")).toBe("chart-GSPC-1D.png");
  });
});
