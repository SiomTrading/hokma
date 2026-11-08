import { formatMoment, formatPercent, formatPrice } from "./format";
import type { PriceChartHandle } from "./PriceChart";
import { openingVisible } from "./PriceChart";
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
  /** The chart on screen: its picture goes between the header and the footer. */
  chart: PriceChartHandle;
}

function token(style: CSSStyleDeclaration, name: string, fallback: string): string {
  return style.getPropertyValue(name).trim() || fallback;
}

/** Where the chart picture lands inside the plot area: as large as fits, without stretching the candles. */
export function fitPlot(source: { width: number; height: number }, box: { x: number; y: number; width: number; height: number }) {
  if (source.width <= 0 || source.height <= 0) return { x: box.x, y: box.y, width: 0, height: 0, scale: 0 };
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const width = source.width * scale;
  const height = source.height * scale;
  return { x: box.x + (box.width - width) / 2, y: box.y + (box.height - height) / 2, width, height, scale };
}

/** The chart that is on screen as a PNG: header with symbol and price, the chart picture, the opening pill and the source. */
export function renderChartPng(data: ChartData, options: PngOptions): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = IMAGE_WIDTH;
  canvas.height = IMAGE_HEIGHT;
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.reject(new Error("canvas unavailable"));

  let shot: HTMLCanvasElement;
  try {
    shot = options.chart.takeScreenshot();
  } catch (error) {
    return Promise.reject(error instanceof Error ? error : new Error("chart unavailable"));
  }

  const style = getComputedStyle(options.root);
  const color = {
    bg: token(style, "--bg", "#101516"),
    t1: token(style, "--t1", "#F0F5F8"),
    t2: token(style, "--t2", "#A8B3B8"),
    t3: token(style, "--t3", "#8A969C"),
    up: token(style, "--up", "#00CCCC"),
    down: token(style, "--down", "#F26038"),
    org: token(style, "--org", "#F26038"),
    orgSub: token(style, "--org-sub", "rgba(242,96,56,0.14)"),
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

  ctx.strokeStyle = color.border;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, HEADER + 0.5);
  ctx.lineTo(IMAGE_WIDTH, HEADER + 0.5);
  ctx.stroke();

  // the chart as it is drawn on screen
  const plot = fitPlot(shot, { x: 0, y: HEADER + 1, width: IMAGE_WIDTH, height: IMAGE_HEIGHT - HEADER - FOOTER - 1 });
  if (plot.width > 0) ctx.drawImage(shot, plot.x, plot.y, plot.width, plot.height);

  // the opening pill on top, at the height of its line
  const opening = data.opening_range;
  if (opening && openingVisible(data.candles, opening)) {
    const y = options.chart.priceToCoordinate(opening.high);
    // the screenshot is in device pixels; the coordinate is in CSS pixels of the chart
    const ratio = typeof window !== "undefined" && window.devicePixelRatio > 0 ? window.devicePixelRatio : 1;
    if (y !== null && plot.width > 0) {
      const lineY = plot.y + y * ratio * plot.scale;
      const text = `${t("chart.opening")} ${formatPrice(opening.high, locale)}`;
      ctx.font = `600 16px ${sans}`;
      const width = ctx.measureText(text).width + 24;
      const height = 28;
      const top = lineY - plot.y > 0.1 * plot.height ? lineY - height - 6 : lineY + 6;
      const right = plot.x + plot.width - (options.chart.priceAxisWidth() + 12) * plot.scale * ratio;
      ctx.fillStyle = color.orgSub;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") ctx.roundRect(right - width, top, width, height, height / 2);
      else ctx.rect(right - width, top, width, height);
      ctx.fill();
      ctx.fillStyle = color.org;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(text, right - 12, top + height / 2);
      ctx.textBaseline = "alphabetic";
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
