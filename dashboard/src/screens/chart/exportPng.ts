import { candleSpan, formatBarTime, formatMoment, formatPercent, formatPrice } from "./format";
import { GRID_LINES, VIEW_H, VIEW_W, layoutChart } from "./geometry";
import type { ChartData } from "./types";

export const IMAGE_WIDTH = 1280;
export const IMAGE_HEIGHT = 720;
const HEADER = 96;
const FOOTER = 34;

type Translate = (key: string, params?: Record<string, string | number>) => string;

export interface PngOptions {
  /** Element that carries the theme tokens (`.hk`). */
  root: Element;
  t: Translate;
  locale: string;
}

function token(style: CSSStyleDeclaration, name: string, fallback: string): string {
  return style.getPropertyValue(name).trim() || fallback;
}

/** Draws the chart that is on screen into a PNG: the same candles, grid, opening line and prices. */
export function renderChartPng(data: ChartData, options: PngOptions): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = IMAGE_WIDTH;
  canvas.height = IMAGE_HEIGHT;
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.reject(new Error("canvas unavailable"));

  const style = getComputedStyle(options.root);
  const color = {
    bg: token(style, "--bg", "#101516"),
    t1: token(style, "--t1", "#F0F5F8"),
    t2: token(style, "--t2", "#A8B3B8"),
    t3: token(style, "--t3", "#8A969C"),
    grid: token(style, "--grid", "rgba(255,255,255,0.06)"),
    up: token(style, "--up", "#4FD69C"),
    down: token(style, "--down", "#FF8A73"),
    org: token(style, "--org", "#F26038"),
    border: token(style, "--border", "rgba(255,255,255,0.09)"),
  };
  const { t, locale } = options;
  const sans = "Montserrat, sans-serif";
  const mono = "JetBrains Mono, monospace";

  ctx.fillStyle = color.bg;
  ctx.fillRect(0, 0, IMAGE_WIDTH, IMAGE_HEIGHT);

  // header: symbol and timeframe, name, last price and change
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = color.t1;
  ctx.font = `600 34px ${mono}`;
  ctx.textAlign = "left";
  ctx.fillText(`${data.symbol} · ${data.timeframe}`, 32, 50);
  ctx.fillStyle = color.t2;
  ctx.font = `400 18px ${sans}`;
  ctx.fillText(t("chart.image.sub", { name: data.name, exchange: data.exchange }), 32, 80);
  const positive = (data.change ?? 0) >= 0;
  ctx.textAlign = "right";
  ctx.fillStyle = positive ? color.up : color.down;
  ctx.font = `600 34px ${mono}`;
  ctx.fillText(formatPrice(data.last_price, locale), IMAGE_WIDTH - 32, 50);
  ctx.font = `500 18px ${mono}`;
  ctx.fillText(formatPercent(data.change_pct, locale), IMAGE_WIDTH - 32, 80);

  // plot
  const top = HEADER;
  const height = IMAGE_HEIGHT - HEADER - FOOTER;
  const sx = IMAGE_WIDTH / VIEW_W;
  const sy = height / VIEW_H;
  const px = (x: number) => x * sx;
  const py = (y: number) => top + y * sy;
  const layout = layoutChart(data.candles, data.opening_range);

  ctx.strokeStyle = color.border;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, top + 0.5);
  ctx.lineTo(IMAGE_WIDTH, top + 0.5);
  ctx.stroke();

  ctx.strokeStyle = color.grid;
  ctx.fillStyle = color.t3;
  ctx.font = `400 15px ${mono}`;
  ctx.textAlign = "left";
  for (const line of layout.grid) {
    ctx.beginPath();
    ctx.moveTo(0, py(line.y));
    ctx.lineTo(IMAGE_WIDTH, py(line.y));
    ctx.stroke();
    ctx.fillText(formatPrice(line.price, locale), 10, py(line.y) - 6);
  }

  for (const shape of layout.shapes) {
    ctx.fillStyle = ctx.strokeStyle = shape.up ? color.up : color.down;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(px(shape.cx), py(shape.wickTop));
    ctx.lineTo(px(shape.cx), py(shape.wickBottom));
    ctx.stroke();
    ctx.fillRect(px(shape.x), py(shape.bodyY), Math.max(1, shape.width * sx), Math.max(1.5, shape.bodyH * sy));
  }

  if (layout.openingY !== null && data.opening_range) {
    const y = py(layout.openingY);
    ctx.strokeStyle = color.org;
    ctx.fillStyle = color.org;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([8, 8]);
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(IMAGE_WIDTH, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = `600 16px ${sans}`;
    ctx.textAlign = "right";
    ctx.fillText(`${t("chart.opening")} ${formatPrice(data.opening_range.high, locale)}`, IMAGE_WIDTH - 16, y - 8);
  }

  // time labels
  const span = candleSpan(data.candles);
  ctx.fillStyle = color.t3;
  ctx.font = `400 14px ${mono}`;
  ctx.textAlign = "center";
  const last = data.candles.length - 1;
  if (last >= 0) {
    for (let i = 0; i < GRID_LINES; i += 1) {
      const index = Math.min(last, Math.round(((i + 0.5) / GRID_LINES) * last));
      ctx.fillText(formatBarTime(data.candles[index]!.t, data.timeframe, data.timezone, locale, span), px(layout.shapes[index]!.cx), top + height - 8);
    }
  }

  // footer: where the data comes from and how fresh it is
  ctx.fillStyle = color.t3;
  ctx.font = `400 14px ${sans}`;
  ctx.textAlign = "left";
  ctx.fillText(t("chart.image.updated", { source: data.source, when: formatMoment(data.last_trade_at ?? data.fetched_at, data.timezone, locale) }), 32, IMAGE_HEIGHT - 12);

  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("png export failed"))), "image/png");
  });
}
