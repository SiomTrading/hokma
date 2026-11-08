import { describe, expect, it } from "vitest";
import { APPROX_ALPHA, BODY_MAX, BODY_MIN, BODY_RADIUS, CandlePaneView, WICK_WIDTH, bodyWidth, candleAlpha, candleShape } from "./customCandles";
import { formatBarTime, formatPercent, formatPrice, safeFileName } from "./format";
import { diffBars, openingVisible, readColors, toCandle } from "./PriceChart";
import type { PriceBar } from "./PriceChart";
import type { OpeningRange } from "./types";

const bar = (i: number, o: number, h: number, l: number, c: number, extra: Partial<PriceBar> = {}): PriceBar => ({ t: 1000 + i * 300, o, h, l, c, v: 10, ...extra });
const opening = (high: number, start = 1000): OpeningRange => ({ high, session_date: "2026-11-04", start, end: start + 1800, complete: true });

describe("candle geometry", () => {
  it("the body takes 60 % of the space per bar, up to 36 px", () => {
    expect(bodyWidth(10)).toBeCloseTo(6, 9);
    expect(bodyWidth(30)).toBeCloseTo(18, 9);
    expect(bodyWidth(60)).toBe(BODY_MAX);
    expect(bodyWidth(200)).toBe(36);
  });

  it("wick 1.5 px, corners 1.5 px, centered on the bar", () => {
    const shape = candleShape(100, 20, { open: 50, high: 30, low: 90, close: 40 }, true);
    expect(WICK_WIDTH).toBe(1.5);
    expect(BODY_RADIUS).toBe(1.5);
    expect(shape.wickWidth).toBe(1.5);
    expect(shape.radius).toBe(1.5);
    expect(shape.width).toBeCloseTo(12, 9);
    expect(shape.x + shape.width / 2).toBeCloseTo(100, 9);
    expect(shape.wickTop).toBe(30);
    expect(shape.wickBottom).toBe(90);
    expect(shape.bodyY).toBe(40);
    expect(shape.bodyH).toBe(10);
  });

  it("a doji still gets a 1.5 px body", () => {
    const shape = candleShape(10, 20, { open: 50, high: 45, low: 55, close: 50 }, true);
    expect(BODY_MIN).toBe(1.5);
    expect(shape.bodyH).toBe(1.5);
  });

  it("corners never exceed half of a very thin body", () => {
    expect(candleShape(10, 2, { open: 1, high: 0, low: 9, close: 8 }, false).radius).toBeCloseTo(0.6, 9);
  });

  it("approximate bars are drawn at 55 % opacity, exact ones fully", () => {
    expect(APPROX_ALPHA).toBe(0.55);
    expect(candleAlpha(true)).toBe(0.55);
    expect(candleAlpha(false)).toBe(1);
    expect(candleAlpha(undefined)).toBe(1);
    expect(candleShape(0, 10, { open: 1, high: 0, low: 3, close: 2 }, true, true).alpha).toBe(0.55);
  });

  it("the series gives high, low and close for the scale, and knows whitespace", () => {
    const view = new CandlePaneView();
    expect(view.priceValueBuilder({ time: 1 as never, open: 1, high: 5, low: 0, close: 3 })).toEqual([5, 0, 3]);
    expect(view.isWhitespace({ time: 1 as never })).toBe(true);
    expect(view.isWhitespace({ time: 1 as never, open: 1, high: 1, low: 1, close: 1 } as never)).toBe(false);
  });

  it("draws each visible candle in its color, with the approximate one dimmed", () => {
    const view = new CandlePaneView();
    const items = [
      { x: 10, time: 0, barColor: "", originalData: { time: 1 as never, open: 10, high: 12, low: 9, close: 11 } },
      { x: 30, time: 1, barColor: "", originalData: { time: 2 as never, open: 11, high: 11.5, low: 8, close: 9, approx: true } },
    ];
    view.update({ bars: items, barSpacing: 20, visibleRange: { from: 0, to: 2 }, conflationFactor: 1 }, { ...view.defaultOptions(), upColor: "UP", downColor: "DOWN" });
    const fills: Array<{ style: string; alpha: number }> = [];
    const ctx = {
      fillStyle: "",
      globalAlpha: 1,
      fillRect() {
        fills.push({ style: ctx.fillStyle, alpha: ctx.globalAlpha });
      },
      beginPath() {},
      roundRect() {},
      rect() {},
      fill() {
        fills.push({ style: ctx.fillStyle, alpha: ctx.globalAlpha });
      },
    };
    const target = { useBitmapCoordinateSpace: (draw: (scope: unknown) => void) => draw({ context: ctx, horizontalPixelRatio: 1, verticalPixelRatio: 1 }) };
    view.renderer().draw(target as never, (price: number) => (100 - price) as never, false);
    expect(fills).toEqual([
      { style: "UP", alpha: 1 },
      { style: "UP", alpha: 1 },
      { style: "DOWN", alpha: 0.55 },
      { style: "DOWN", alpha: 0.55 },
    ]);
    expect(ctx.globalAlpha).toBe(1);
  });
});

describe("theme colors", () => {
  it("come from the CSS variables of the .hk element, never from the code", () => {
    const root = document.createElement("div");
    root.className = "hk";
    root.style.setProperty("--up", "rgb(1, 2, 3)");
    root.style.setProperty("--down", "rgb(4, 5, 6)");
    root.style.setProperty("--grid", "rgba(7, 8, 9, 0.1)");
    root.style.setProperty("--border", "rgb(10, 11, 12)");
    root.style.setProperty("--t3", "rgb(13, 14, 15)");
    root.style.setProperty("--org", "rgb(16, 17, 18)");
    document.body.appendChild(root);
    expect(readColors(root)).toEqual({ up: "rgb(1, 2, 3)", down: "rgb(4, 5, 6)", grid: "rgba(7, 8, 9, 0.1)", border: "rgb(10, 11, 12)", t3: "rgb(13, 14, 15)", org: "rgb(16, 17, 18)" });
    root.remove();
  });

  it("a missing token reads as transparent, never as an empty color", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    expect(readColors(root).up).toBe("transparent");
    root.remove();
  });
});

describe("bars to the series", () => {
  const base = [bar(0, 10, 14, 9, 12), bar(1, 12, 13, 8, 9), bar(2, 9, 11, 9, 11)];

  it("by time the series gets the start; by index it gets i and keeps the approx flag", () => {
    expect(toCandle(base[0]!, "time")).toEqual({ time: 1000, open: 10, high: 14, low: 9, close: 12, approx: false });
    expect(toCandle(bar(0, 1, 2, 0, 1, { i: 7, approx: true }), "index")).toEqual({ time: 7, open: 1, high: 2, low: 0, close: 1, approx: true });
  });

  it("the first set is a snapshot", () => {
    expect(diffBars([], base, "time")).toEqual({ kind: "snapshot" });
  });

  it("a change of the last bar or a new bar is live", () => {
    const moved = [...base.slice(0, 2), { ...base[2]!, c: 12, h: 12 }];
    expect(diffBars(base, moved, "time")).toEqual({ kind: "live", changed: [moved[2]] });
    const added = [...base, bar(3, 11, 12, 10, 11.5)];
    expect(diffBars(base, added, "time")).toEqual({ kind: "live", changed: [added[3]] });
    expect(diffBars(base, base.slice(), "time")).toEqual({ kind: "none" });
  });

  it("old bars falling off the front while one is added is still live", () => {
    const next = [...base.slice(1), bar(3, 11, 12, 10, 11.5)];
    expect(diffBars(base, next, "time")).toEqual({ kind: "live", changed: [next[2]] });
  });

  it("other bars, or a change in the middle, are a new snapshot", () => {
    expect(diffBars(base, [bar(10, 1, 2, 0, 1)], "time")).toEqual({ kind: "snapshot" });
    const middle = [base[0]!, { ...base[1]!, c: 10 }, base[2]!];
    expect(diffBars(base, middle, "time")).toEqual({ kind: "snapshot" });
  });

  it("range bars are matched by their index, not by their time", () => {
    const range = [bar(0, 1, 2, 0, 2, { i: 0 }), { ...bar(0, 2, 3, 2, 3), i: 1 }];
    const next = [...range, { ...bar(0, 3, 4, 3, 4), i: 2 }];
    expect(diffBars(range, next, "index")).toEqual({ kind: "live", changed: [next[2]] });
  });
});

describe("opening line", () => {
  const base = [bar(0, 10, 14, 9, 12), bar(1, 12, 13, 8, 9), bar(2, 9, 11, 9, 11)];

  it("is drawn when its session starts among the bars shown", () => {
    expect(openingVisible(base, opening(14))).toBe(true);
  });

  it("is hidden when its session starts before the bars, or without bars", () => {
    expect(openingVisible(base, opening(14, 10))).toBe(false);
    expect(openingVisible([], opening(14))).toBe(false);
    expect(openingVisible(base, null)).toBe(false);
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
