import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { ColorType, CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import type {
  AutoscaleInfo,
  DeepPartial,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  MouseEventParams,
  SeriesOptionsCommon,
  Time,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";
import { useI18n } from "@/i18n";
import { CandlePaneView } from "./customCandles";
import type { CandleData, CandleSeriesOptions } from "./customCandles";
import { restoreView, sameView, viewFromChart } from "./chartView";
import type { ChartViewState } from "./chartState";
import { candleSpan, formatBarTime, formatPrice, formatSessionDate, formatVolume } from "./format";
import type { OpeningRange, Timeframe } from "./types";

/** One bar handed to the chart. `t` is its real start (epoch s); `i` its index when the chart runs by index. */
export interface PriceBar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  /** Built from coarser data: drawn dimmed. */
  approx?: boolean;
  /** Position of the bar when `axis` is "index" (range bars). */
  i?: number;
}

/** What the PNG export needs from the chart on screen. */
export interface PriceChartHandle {
  /** The chart as drawn: candles, axes and the opening line. */
  takeScreenshot(): HTMLCanvasElement;
  /** Y of a price inside the screenshot (CSS pixels from the top of the chart), or null when off scale. */
  priceToCoordinate(price: number): number | null;
  /** Width of the price axis on the right, in CSS pixels. */
  priceAxisWidth(): number;
}

export interface PriceChartProps {
  bars: PriceBar[];
  /** Label format of the bars (see formatBarTime). */
  timeframe: Timeframe;
  /** Time zone of the symbol: every time on the chart is shown in it. */
  timezone: string;
  /** "time" places bars by their start; "index" places them by `i` and still labels them with their real time. */
  axis?: "time" | "index";
  /** High of the opening range: dashed line and its pill. Drawn only when its session is among the bars. */
  opening?: OpeningRange | null;
  /** Accessible name of the chart. */
  label: string;
  /**
   * The view to draw the first snapshot with (zoom, scroll, price scale), or null to fit all the bars. It is applied
   * once, with the first snapshot that has bars; later snapshots (a reconnection) keep the view the student has.
   */
  initialView?: ChartViewState | null;
  /** The student moved the view: wheel, drag, double click, price scale. Not called for what the chart does by itself. */
  onViewChange?: (view: ChartViewState) => void;
}

/**
 * Where the chart instance comes from. Tests swap `createChart` for a recording stand-in (jsdom has no canvas); the
 * app never changes it.
 */
export const chartEngine = { createChart };

/** Name of each color token the chart reads from the theme. */
const TOKENS = { up: "--up", down: "--down", grid: "--grid", border: "--border", t3: "--t3", org: "--org" } as const;
type Colors = Record<keyof typeof TOKENS, string>;
const MONO = "'JetBrains Mono', ui-monospace, monospace";
/** Space above the highest candle and below the lowest, as a share of the pane. */
const MARGINS = { top: 0.09, bottom: 0.06 };
const PRICE_AXIS = 78;
/** Spacing of the price labels: about five horizontal grid lines on a pane of usual height, like the design. */
const PRICE_TICK_DENSITY = 7;
const PILL_GAP = 12;

/**
 * The theme colors, read from the `.hk` element that carries the tokens. A token that is missing reads as
 * "transparent": the chart library refuses an empty color.
 */
export function readColors(root: Element): Colors {
  const style = getComputedStyle(root);
  const out = {} as Colors;
  for (const [key, name] of Object.entries(TOKENS) as Array<[keyof Colors, string]>) out[key] = style.getPropertyValue(name).trim() || "transparent";
  return out;
}

/** The opening line is drawn only when its session starts inside the bars shown. */
export function openingVisible(bars: PriceBar[], opening: OpeningRange | null | undefined): opening is OpeningRange {
  return !!opening && bars.length > 0 && opening.start >= bars[0]!.t && opening.start <= bars[bars.length - 1]!.t;
}

export function toCandle(bar: PriceBar, axis: "time" | "index"): CandleData {
  const time = (axis === "index" ? (bar.i ?? 0) : bar.t) as UTCTimestamp;
  return { time, open: bar.o, high: bar.h, low: bar.l, close: bar.c, approx: bar.approx === true };
}

const keyOf = (bar: PriceBar, axis: "time" | "index") => (axis === "index" ? (bar.i ?? 0) : bar.t);
const sameBar = (a: PriceBar, b: PriceBar) => a.t === b.t && a.o === b.o && a.h === b.h && a.l === b.l && a.c === b.c && a.v === b.v && !!a.approx === !!b.approx && a.i === b.i;

/**
 * How new bars reach the series. A live change (the last bar moves, new bars are appended, old ones fall off the
 * front) goes through `update`; anything else is a new snapshot: the whole set and a fit to the width.
 */
export type BarsChange = { kind: "snapshot" } | { kind: "live"; changed: PriceBar[] } | { kind: "none" };

export function diffBars(prev: PriceBar[], next: PriceBar[], axis: "time" | "index"): BarsChange {
  if (prev.length === 0 || next.length === 0) return next.length === 0 && prev.length === 0 ? { kind: "none" } : { kind: "snapshot" };
  const first = keyOf(next[0]!, axis);
  const offset = prev.findIndex((b) => keyOf(b, axis) === first);
  if (offset < 0) return { kind: "snapshot" };
  const overlap = prev.length - offset;
  if (next.length < overlap) return { kind: "snapshot" };
  for (let k = 0; k < overlap - 1; k += 1) {
    const a = prev[offset + k]!;
    const b = next[k]!;
    if (keyOf(a, axis) !== keyOf(b, axis) || !sameBar(a, b)) return { kind: "snapshot" };
  }
  const tailPrev = prev[prev.length - 1]!;
  const tailNext = next[overlap - 1]!;
  if (keyOf(tailPrev, axis) !== keyOf(tailNext, axis)) return { kind: "snapshot" };
  const changed = sameBar(tailPrev, tailNext) ? [] : [tailNext];
  changed.push(...next.slice(overlap));
  return changed.length ? { kind: "live", changed } : { kind: "none" };
}

interface Hover {
  bar: PriceBar;
}

/**
 * The price chart: one chart instance for the life of the component, candles drawn by a custom series that keeps
 * the Hokma look, the OHLCV readout and the opening pill in HTML on top. Wheel zooms, dragging scrolls and a double
 * click on the time axis fits all bars again.
 */
export const PriceChart = forwardRef<PriceChartHandle, PriceChartProps>(function PriceChart(
  { bars, timeframe, timezone, axis = "time", opening = null, label, initialView = null, onViewChange },
  ref,
) {
  const { t, locale } = useI18n();
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Custom", Time, CandleData | WhitespaceData<Time>, CandleSeriesOptions, DeepPartial<CandleSeriesOptions & SeriesOptionsCommon>> | null>(null);
  const lineRef = useRef<IPriceLine | null>(null);
  const shownRef = useRef<PriceBar[]>([]);
  const byKeyRef = useRef(new Map<number, PriceBar>());
  const [hover, setHover] = useState<Hover | null>(null);
  const [pill, setPill] = useState<{ top: number; right: number } | null>(null);

  const span = candleSpan(bars);
  const showOpening = openingVisible(bars, opening);
  const openingHigh = showOpening ? opening.high : null;

  // Read by the formatters the chart calls on its own schedule.
  const fmt = useRef({ timeframe, timezone, locale, span, axis });
  fmt.current = { timeframe, timezone, locale, span, axis };
  const realTime = useCallback((time: Time): number | null => {
    const key = typeof time === "number" ? time : null;
    if (key === null) return null;
    if (fmt.current.axis === "time") return key;
    return byKeyRef.current.get(key)?.t ?? null;
  }, []);

  const openingRef = useRef<number | null>(openingHigh);
  openingRef.current = openingHigh;

  // The view: read from the chart on demand, applied once at the first snapshot, never written back by the chart itself.
  const bodyRef = useRef<HTMLDivElement>(null);
  const initialViewRef = useRef(initialView);
  const restoredRef = useRef(false);
  const onViewRef = useRef(onViewChange);
  onViewRef.current = onViewChange;
  const programmatic = useRef(false);
  /** What to do with the view once the chart has a width: a tab that mounts hidden has none, and spacing and fit mean nothing then. */
  const deferred = useRef<(() => void) | null>(null);
  /** The view the chart was left in by code (fit or restore), or the last one reported: what "moved" is measured from. */
  const baseline = useRef<ChartViewState | null>(null);
  const pointerDown = useRef(false);
  const activeUntil = useRef(0);
  /** Calls made on the series, mirrored in the DOM (`data-set-data`, `data-updates`) so the E2E can count them. */
  const calls = useRef({ setData: 0, updates: 0 });
  const countCalls = useCallback(() => {
    bodyRef.current?.setAttribute("data-set-data", String(calls.current.setData));
    bodyRef.current?.setAttribute("data-updates", String(calls.current.updates));
    bodyRef.current?.setAttribute("data-last-bar", String(shownRef.current[shownRef.current.length - 1]?.t ?? ""));
  }, []);

  /** The view as the chart has it now, or null while there is nothing to read. */
  const readView = useCallback(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return null;
    return viewFromChart(chart, shownRef.current);
  }, []);
  /** Mirrors the live view in the DOM so tests and support can read the real state of the chart. */
  const publishView = useCallback(() => {
    const body = bodyRef.current;
    const read = readView();
    if (!body || !read) return;
    body.setAttribute("data-view", JSON.stringify({ ...read.view, priceRange: read.priceRange }));
  }, [readView]);
  const emitView = useCallback(() => {
    if (programmatic.current) return;
    const read = readView();
    if (!read) return;
    publishView();
    // A click that moved nothing is not a new view.
    if (baseline.current && sameView(baseline.current, read.view)) return;
    baseline.current = read.view;
    onViewRef.current?.(read.view);
  }, [readView, publishView]);
  const touch = useCallback((ms: number) => {
    activeUntil.current = Date.now() + ms;
  }, []);

  // The page is going away: hand over the view as it is right now, before the store writes what it has waiting (the
  // capture listener runs ahead of the store's own). Not done on unmount: a new interval or symbol unmounts the chart
  // too, and its old view must not be saved under the new one.
  useEffect(() => {
    const now = () => emitView();
    const hidden = () => {
      if (document.visibilityState === "hidden") emitView();
    };
    window.addEventListener("pagehide", now, true);
    document.addEventListener("visibilitychange", hidden, true);
    return () => {
      window.removeEventListener("pagehide", now, true);
      document.removeEventListener("visibilitychange", hidden, true);
    };
  }, [emitView]);

  const placePill = useCallback(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    const host = hostRef.current;
    const price = openingRef.current;
    if (!series || !chart || !host || price === null) {
      setPill(null);
      return;
    }
    const y = series.priceToCoordinate(price);
    const paneHeight = host.clientHeight; // 0 until laid out: then there is nothing to clip against
    if (y === null || (paneHeight > 0 && (y < 0 || y > paneHeight))) {
      setPill(null);
      return;
    }
    const top = paneHeight === 0 || y / paneHeight > 0.1 ? y - 26 : y + 6;
    const right = (chart.priceScale("right").width() || PRICE_AXIS) + PILL_GAP;
    setPill((old) => (old && old.top === top && old.right === right ? old : { top, right }));
  }, []);

  // The chart lives as long as the component.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const root = host.closest(".hk") ?? document.documentElement;
    const colors = readColors(root);
    const chart = chartEngine.createChart(host, {
      width: host.clientWidth,
      height: host.clientHeight,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: colors.t3, fontSize: 10, fontFamily: MONO },
      grid: { vertLines: { visible: false }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border, minimumWidth: PRICE_AXIS, scaleMargins: MARGINS, tickMarkDensity: PRICE_TICK_DENSITY },
      timeScale: {
        borderColor: colors.border,
        timeVisible: true,
        allowBoldLabels: false,
        secondsVisible: false,
        tickMarkFormatter: (time: Time) => {
          const real = realTime(time);
          const f = fmt.current;
          return real === null ? null : formatBarTime(real, f.timeframe, f.timezone, f.locale, f.span);
        },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: colors.t3, width: 1, style: LineStyle.SparseDotted, labelVisible: false },
        horzLine: { visible: false, labelVisible: false },
      },
      localization: {
        locale: fmt.current.locale,
        priceFormatter: (price: number) => formatPrice(price, fmt.current.locale),
        timeFormatter: (time: Time) => {
          const real = realTime(time);
          const f = fmt.current;
          return real === null ? "" : formatBarTime(real, f.timeframe, f.timezone, f.locale, f.span);
        },
      },
    });
    const series = chart.addCustomSeries(new CandlePaneView(), {
      upColor: colors.up,
      downColor: colors.down,
      color: colors.t3,
      priceLineVisible: false,
      lastValueVisible: false,
      autoscaleInfoProvider: (base: () => AutoscaleInfo | null) => {
        const info = base();
        const price = openingRef.current;
        if (!info || !info.priceRange || price === null) return info;
        return { ...info, priceRange: { minValue: Math.min(info.priceRange.minValue, price), maxValue: Math.max(info.priceRange.maxValue, price) } };
      },
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const onMove = (param: MouseEventParams<Time>) => {
      if (param.time === undefined || !param.point) {
        setHover(null);
        return;
      }
      const key = typeof param.time === "number" ? param.time : NaN;
      const bar = byKeyRef.current.get(key);
      setHover(bar ? { bar } : null);
    };
    chart.subscribeCrosshairMove(onMove);
    const onRange = () => {
      placePill();
      if (programmatic.current) return;
      publishView();
      // Scrolling goes on after the pointer is up (kinetic scroll): the activity window covers it.
      if (pointerDown.current || Date.now() < activeUntil.current) emitView();
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    chart.timeScale().subscribeSizeChange(placePill);

    // The library takes the new width on its next frame: wait until its time scale has one before placing the view.
    const placeDeferred = (tries: number) => {
      const later = deferred.current;
      if (!later || chartRef.current !== chart) return;
      if (chart.timeScale().width() <= 1) {
        if (tries < 30) requestAnimationFrame(() => placeDeferred(tries + 1));
        return;
      }
      deferred.current = null;
      programmatic.current = true;
      try {
        later();
      } finally {
        programmatic.current = false;
      }
      baseline.current = readView()?.view ?? null;
      publishView();
    };

    const resize =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            chart.resize(host.clientWidth, host.clientHeight);
            placePill();
            if (host.clientWidth > 0) placeDeferred(0);
          });
    resize?.observe(host);

    // A theme switch changes the tokens: read them again.
    const theme = new MutationObserver(() => {
      const next = readColors(root);
      chart.applyOptions({
        layout: { textColor: next.t3 },
        grid: { horzLines: { color: next.grid } },
        rightPriceScale: { borderColor: next.border },
        timeScale: { borderColor: next.border },
        crosshair: { vertLine: { color: next.t3 } },
      });
      series.applyOptions({ upColor: next.up, downColor: next.down, color: next.t3 });
      lineRef.current?.applyOptions({ color: next.org });
    });
    theme.observe(root, { attributes: true, attributeFilter: ["class"] });

    return () => {
      theme.disconnect();
      resize?.disconnect();
      chart.unsubscribeCrosshairMove(onMove);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      chart.timeScale().unsubscribeSizeChange(placePill);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      lineRef.current = null;
      shownRef.current = [];
    };
  }, [placePill, realTime, publishView, emitView]);

  // Locale changes reach the price axis.
  useEffect(() => {
    chartRef.current?.applyOptions({ localization: { locale } });
  }, [locale]);

  // Bars: a snapshot replaces everything and fits the width; live changes only touch the tail.
  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    const map = new Map<number, PriceBar>();
    for (const bar of bars) map.set(keyOf(bar, axis), bar);
    byKeyRef.current = map;
    const change = diffBars(shownRef.current, bars, axis);
    if (change.kind === "snapshot") {
      const first = !restoredRef.current;
      // A reconnection brings the same chart again: keep what the student has on screen, do not fit.
      const keep = first ? null : readView();
      programmatic.current = true;
      try {
        series.setData(bars.map((bar) => toCandle(bar, axis)));
        calls.current.setData += 1;
        shownRef.current = bars;
        if (first && bars.length > 0) {
          restoredRef.current = true;
          const wanted = initialViewRef.current;
          const place = () => (wanted ? restoreView(chart, wanted, shownRef.current) : chart.timeScale().fitContent());
          const host = hostRef.current;
          if (!host || host.clientWidth > 0 || typeof ResizeObserver === "undefined") place();
          else deferred.current = place; // mounted hidden: the first resize with a width places it
        } else if (keep) {
          restoreView(chart, keep.view, bars);
        } else if (first) {
          chart.timeScale().fitContent();
        }
      } finally {
        programmatic.current = false;
      }
      baseline.current = readView()?.view ?? null;
      publishView();
    } else if (change.kind === "live") {
      for (const bar of change.changed) {
        series.update(toCandle(bar, axis));
        calls.current.updates += 1;
      }
    }
    shownRef.current = bars;
    countCalls();
    placePill();
  }, [bars, axis, placePill, readView, publishView, countCalls]);

  // The opening line follows the opening high.
  useEffect(() => {
    const series = seriesRef.current;
    const host = hostRef.current;
    if (!series || !host) return;
    if (lineRef.current) {
      series.removePriceLine(lineRef.current);
      lineRef.current = null;
    }
    if (openingHigh !== null) {
      const colors = readColors(host.closest(".hk") ?? document.documentElement);
      lineRef.current = series.createPriceLine({
        price: openingHigh,
        color: colors.org,
        lineWidth: 1,
        lineStyle: LineStyle.LargeDashed,
        lineVisible: true,
        axisLabelVisible: false,
        title: "",
      });
    }
    placePill();
  }, [openingHigh, placePill]);

  useImperativeHandle(
    ref,
    () => ({
      takeScreenshot: () => {
        const chart = chartRef.current;
        if (!chart) throw new Error("chart not ready");
        return chart.takeScreenshot();
      },
      priceToCoordinate: (price: number) => seriesRef.current?.priceToCoordinate(price) ?? null,
      priceAxisWidth: () => chartRef.current?.priceScale("right").width() || PRICE_AXIS,
    }),
    [],
  );

  const hovered = hover?.bar;
  const readout = useMemo(
    () =>
      hovered
        ? t("chart.hover", {
            time: formatBarTime(hovered.t, timeframe, timezone, locale, span),
            o: formatPrice(hovered.o, locale),
            h: formatPrice(hovered.h, locale),
            l: formatPrice(hovered.l, locale),
            c: formatPrice(hovered.c, locale),
            v: formatVolume(hovered.v, locale),
          })
        : null,
    [hovered, t, timeframe, timezone, locale, span],
  );

  return (
    <div
      ref={bodyRef}
      data-testid="chart-body"
      data-bars={bars.length}
      tabIndex={0}
      style={{ flex: 1, display: "flex", minHeight: 260, minWidth: 0, position: "relative", outlineOffset: -2 }}
      onPointerDown={() => {
        pointerDown.current = true;
      }}
      onPointerUp={() => {
        pointerDown.current = false;
        touch(1500);
        placePill();
        emitView();
      }}
      onPointerCancel={() => {
        pointerDown.current = false;
        touch(1500);
        emitView();
      }}
      onWheel={() => {
        touch(500);
        placePill();
        emitView();
      }}
      onDoubleClick={() => {
        touch(500);
        emitView();
      }}
    >
      <div ref={hostRef} data-testid="price-chart" role="img" aria-label={label} style={{ position: "absolute", inset: 0 }} />
      {showOpening && pill ? (
        <span
          className="pill org"
          data-testid="opening-pill"
          data-price={opening.high}
          aria-label={t("chart.opening.aria", { date: formatSessionDate(opening.session_date, locale), price: formatPrice(opening.high, locale) })}
          style={{ position: "absolute", right: pill.right, top: pill.top, backdropFilter: "blur(6px)", WebkitBackdropFilter: "blur(6px)", pointerEvents: "none", zIndex: 3 }}
        >
          {t("chart.opening")} <span className="mono">{formatPrice(opening.high, locale)}</span>
        </span>
      ) : null}
      {readout ? (
        <div
          className="mono"
          data-testid="chart-readout"
          style={{ position: "absolute", top: 6, left: 8, right: PRICE_AXIS + 8, fontSize: 11, color: "var(--t2)", pointerEvents: "none", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", zIndex: 3 }}
        >
          {readout}
        </div>
      ) : null}
    </div>
  );
});
