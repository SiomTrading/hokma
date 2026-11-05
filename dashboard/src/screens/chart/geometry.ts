import type { Candle, OpeningRange } from "./types";

/** The drawing is done on a 1000 x 400 canvas that the SVG stretches to its box, like the design. */
export const VIEW_W = 1000;
export const VIEW_H = 400;
const PAD_X = 12;
const PAD_TOP = 18;
const PAD_BOTTOM = 30; // room for the time labels
export const GRID_LINES = 5;

export interface CandleShape {
  /** Index in the visible candles. */
  index: number;
  /** Center of the candle. */
  cx: number;
  x: number;
  width: number;
  bodyY: number;
  bodyH: number;
  wickTop: number;
  wickBottom: number;
  up: boolean;
}

export interface GridLine {
  y: number;
  price: number;
}

export interface ChartLayout {
  shapes: CandleShape[];
  grid: GridLine[];
  /** Y of the opening-high line, or null when it is not drawn. */
  openingY: number | null;
  min: number;
  max: number;
  step: number;
  /** Price at a y position of the canvas. */
  priceAt: (y: number) => number;
}

/** The opening line is drawn only when its session starts inside the candles shown. */
export function openingVisible(candles: Candle[], opening: OpeningRange | null): opening is OpeningRange {
  return !!opening && candles.length > 0 && opening.start >= candles[0]!.t && opening.start <= candles[candles.length - 1]!.t;
}

export function layoutChart(candles: Candle[], opening: OpeningRange | null): ChartLayout {
  const plotTop = PAD_TOP;
  const plotBottom = VIEW_H - PAD_BOTTOM;
  const showOpening = openingVisible(candles, opening);
  let min = Infinity;
  let max = -Infinity;
  for (const c of candles) {
    if (c.l < min) min = c.l;
    if (c.h > max) max = c.h;
  }
  if (showOpening) {
    min = Math.min(min, opening.high);
    max = Math.max(max, opening.high);
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    min = 0;
    max = 1;
  }
  if (max === min) {
    const pad = Math.abs(max) * 0.005 || 0.5;
    min -= pad;
    max += pad;
  }
  const margin = (max - min) * 0.06;
  min -= margin;
  max += margin;

  const yOf = (price: number) => plotTop + ((max - price) / (max - min)) * (plotBottom - plotTop);
  const priceAt = (y: number) => max - ((y - plotTop) / (plotBottom - plotTop)) * (max - min);

  const count = Math.max(candles.length, 1);
  const step = (VIEW_W - PAD_X * 2) / count;
  const width = Math.min(step * 0.6, 36);

  const shapes: CandleShape[] = candles.map((c, index) => {
    const cx = PAD_X + (index + 0.5) * step;
    const top = yOf(Math.max(c.o, c.c));
    const bottom = yOf(Math.min(c.o, c.c));
    return {
      index,
      cx,
      x: cx - width / 2,
      width,
      bodyY: top,
      bodyH: Math.max(1.5, bottom - top),
      wickTop: yOf(c.h),
      wickBottom: yOf(c.l),
      up: c.c >= c.o,
    };
  });

  const grid: GridLine[] = [];
  for (let i = 0; i < GRID_LINES; i += 1) {
    const y = plotTop + ((plotBottom - plotTop) * (i + 0.5)) / GRID_LINES;
    grid.push({ y, price: priceAt(y) });
  }

  return { shapes, grid, openingY: showOpening ? yOf(opening.high) : null, min, max, step, priceAt };
}

/** Index of the candle under an x position of the canvas, or -1. */
export function candleIndexAt(x: number, count: number): number {
  if (count <= 0) return -1;
  const step = (VIEW_W - PAD_X * 2) / count;
  const index = Math.floor((x - PAD_X) / step);
  return index < 0 || index >= count ? -1 : index;
}
