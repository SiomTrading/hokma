import type {
  CustomData,
  CustomSeriesOptions,
  CustomSeriesPricePlotValues,
  CustomSeriesWhitespaceData,
  ICustomSeriesPaneRenderer,
  ICustomSeriesPaneView,
  PaneRendererCustomData,
  PriceToCoordinateConverter,
  Time,
} from "lightweight-charts";
import { customSeriesDefaultOptions } from "lightweight-charts";

/** Body width as a share of the space per bar, and its cap in pixels. */
export const BODY_SHARE = 0.6;
export const BODY_MAX = 36;
/** Width of the wick, corner radius of the body and the smallest body a doji gets, in pixels. */
export const WICK_WIDTH = 1.5;
export const BODY_RADIUS = 1.5;
export const BODY_MIN = 1.5;
/** Opacity of bars that are approximated from coarser data. */
export const APPROX_ALPHA = 0.55;

/** One candle as the series stores it. */
export interface CandleData extends CustomData<Time> {
  open: number;
  high: number;
  low: number;
  close: number;
  /** Drawn at {@link APPROX_ALPHA} when true. */
  approx?: boolean;
}

export interface CandleSeriesOptions extends CustomSeriesOptions {
  upColor: string;
  downColor: string;
}

/** Where one candle goes on the pane, in CSS pixels. */
export interface CandleShape {
  cx: number;
  x: number;
  width: number;
  bodyY: number;
  bodyH: number;
  wickTop: number;
  wickBottom: number;
  wickWidth: number;
  radius: number;
  up: boolean;
  alpha: number;
}

export function bodyWidth(barSpacing: number): number {
  return Math.min(barSpacing * BODY_SHARE, BODY_MAX);
}

export function candleAlpha(approx: boolean | undefined): number {
  return approx ? APPROX_ALPHA : 1;
}

/**
 * The geometry of one candle from its center, the space per bar and the y of its four prices. Pure, so the look of
 * the candles is tested without a canvas.
 */
export function candleShape(
  cx: number,
  barSpacing: number,
  y: { open: number; high: number; low: number; close: number },
  up: boolean,
  approx?: boolean,
): CandleShape {
  const width = bodyWidth(barSpacing);
  const top = Math.min(y.open, y.close);
  const bottom = Math.max(y.open, y.close);
  return {
    cx,
    x: cx - width / 2,
    width,
    bodyY: top,
    bodyH: Math.max(BODY_MIN, bottom - top),
    wickTop: Math.min(y.high, y.low),
    wickBottom: Math.max(y.high, y.low),
    wickWidth: WICK_WIDTH,
    radius: Math.min(BODY_RADIUS, width / 2),
    up,
    alpha: candleAlpha(approx),
  };
}

type DrawTarget = Parameters<ICustomSeriesPaneRenderer["draw"]>[0];

class CandleRenderer implements ICustomSeriesPaneRenderer {
  data: PaneRendererCustomData<Time, CandleData> | null = null;
  options: CandleSeriesOptions | null = null;

  draw(target: DrawTarget, priceToCoordinate: PriceToCoordinateConverter): void {
    const data = this.data;
    const options = this.options;
    if (!data || !options || !data.visibleRange) return;
    const spacing = data.barSpacing * Math.max(1, data.conflationFactor);
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      for (let i = data.visibleRange!.from; i < data.visibleRange!.to; i += 1) {
        const item = data.bars[i];
        if (!item) continue;
        const bar = item.originalData;
        const yo = priceToCoordinate(bar.open);
        const yh = priceToCoordinate(bar.high);
        const yl = priceToCoordinate(bar.low);
        const yc = priceToCoordinate(bar.close);
        if (yo === null || yh === null || yl === null || yc === null) continue;
        const shape = candleShape(item.x, spacing, { open: yo, high: yh, low: yl, close: yc }, bar.close >= bar.open, bar.approx);
        ctx.globalAlpha = shape.alpha;
        ctx.fillStyle = shape.up ? options.upColor : options.downColor;
        const wick = shape.wickWidth * hr;
        ctx.fillRect(shape.cx * hr - wick / 2, shape.wickTop * vr, wick, Math.max(vr, (shape.wickBottom - shape.wickTop) * vr));
        const x = shape.x * hr;
        const yTop = shape.bodyY * vr;
        const w = Math.max(hr, shape.width * hr);
        const h = shape.bodyH * vr;
        ctx.beginPath();
        if (typeof ctx.roundRect === "function") ctx.roundRect(x, yTop, w, h, Math.min(shape.radius * hr, w / 2, h / 2));
        else ctx.rect(x, yTop, w, h);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    });
  }
}

/** Candles with rounded bodies, thin wicks and dimmed approximate bars: the look of the Hokma chart. */
export class CandlePaneView implements ICustomSeriesPaneView<Time, CandleData, CandleSeriesOptions> {
  private readonly paint = new CandleRenderer();

  renderer(): ICustomSeriesPaneRenderer {
    return this.paint;
  }

  update(data: PaneRendererCustomData<Time, CandleData>, options: CandleSeriesOptions): void {
    this.paint.data = data;
    this.paint.options = options;
  }

  priceValueBuilder(bar: CandleData): CustomSeriesPricePlotValues {
    return [bar.high, bar.low, bar.close];
  }

  isWhitespace(bar: CandleData | CustomSeriesWhitespaceData<Time>): bar is CustomSeriesWhitespaceData<Time> {
    return (bar as Partial<CandleData>).close === undefined;
  }

  defaultOptions(): CandleSeriesOptions {
    return { ...customSeriesDefaultOptions, upColor: "transparent", downColor: "transparent" };
  }
}
