/**
 * Stand-in for the chart instance in jsdom, which has no canvas. In a test file:
 *
 *   beforeEach(useFakeCharts);
 *
 * Every chart and series it creates is kept in `charts` with spies on the calls the price chart makes (addCustomSeries,
 * setData, update, createPriceLine, fitContent, resize, remove, takeScreenshot, priceToCoordinate), so tests can check
 * what was drawn and drive the crosshair.
 */
import { vi } from "vitest";
import { chartEngine } from "./PriceChart";

type Handler = (param: unknown) => void;

export interface FakeSeries {
  view: unknown;
  options: Record<string, unknown>;
  data: Array<Record<string, unknown>>;
  setData: ReturnType<typeof vi.fn>;
  update: ReturnType<typeof vi.fn>;
  applyOptions: ReturnType<typeof vi.fn>;
  createPriceLine: ReturnType<typeof vi.fn>;
  removePriceLine: ReturnType<typeof vi.fn>;
  priceToCoordinate: ReturnType<typeof vi.fn>;
  lines: Array<Record<string, unknown>>;
}

/** What the chart "has on screen": the view the price chart reads and writes. Tests move it like the student would. */
export interface FakeView {
  range: { from: number; to: number } | null;
  spacing: number;
  /** Bars from the last bar to the right edge; negative when scrolled back. */
  offset: number;
  autoScale: boolean;
  priceRange: { from: number; to: number } | null;
}

export interface FakeChart {
  container: HTMLElement;
  view: FakeView;
  scrollToPosition: ReturnType<typeof vi.fn>;
  applyTimeScaleOptions: ReturnType<typeof vi.fn>;
  setVisibleLogicalRange: ReturnType<typeof vi.fn>;
  setAutoScale: ReturnType<typeof vi.fn>;
  setVisibleRange: ReturnType<typeof vi.fn>;
  /** Tells the subscribers of the visible range that it changed. */
  fireRange(): void;
  options: Record<string, unknown>;
  series: FakeSeries[];
  crosshair: Set<Handler>;
  addCustomSeries: ReturnType<typeof vi.fn>;
  applyOptions: ReturnType<typeof vi.fn>;
  resize: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  takeScreenshot: ReturnType<typeof vi.fn>;
  fitContent: ReturnType<typeof vi.fn>;
  removed: boolean;
  /** Moves the pointer over a bar (its `time` as the series knows it), or off the chart with undefined. */
  hover(time: number | undefined): void;
}

export const charts: FakeChart[] = [];

/** Options merge like the library does it: nested objects are merged, not replaced. */
function merge(target: Record<string, unknown>, next: Record<string, unknown>): Record<string, unknown> {
  for (const [key, value] of Object.entries(next)) {
    const current = target[key];
    if (value && typeof value === "object" && !Array.isArray(value) && current && typeof current === "object") merge(current as Record<string, unknown>, value as Record<string, unknown>);
    else target[key] = value;
  }
  return target;
}

/** Y the fake series gives a price: 1000 minus the price, so higher prices sit higher, like the real one. */
export const fakeY = (price: number) => 1000 - price;

function makeSeries(view: unknown, options: Record<string, unknown> = {}): FakeSeries {
  const series: FakeSeries = {
    view,
    options: { ...options },
    data: [],
    lines: [],
    setData: vi.fn((data: Array<Record<string, unknown>>) => {
      series.data = data.slice();
    }),
    update: vi.fn((bar: Record<string, unknown>) => {
      const last = series.data[series.data.length - 1];
      if (last && last.time === bar.time) series.data[series.data.length - 1] = bar;
      else series.data.push(bar);
    }),
    applyOptions: vi.fn((next: Record<string, unknown>) => merge(series.options, next)),
    createPriceLine: vi.fn((line: Record<string, unknown>) => {
      const handle = { options: () => line, applyOptions: (next: Record<string, unknown>) => Object.assign(line, next) };
      series.lines.push(line);
      return handle;
    }),
    removePriceLine: vi.fn((handle: { options: () => Record<string, unknown> }) => {
      series.lines = series.lines.filter((line) => line !== handle.options());
    }),
    priceToCoordinate: vi.fn((price: number) => fakeY(price)),
  };
  return series;
}

export function createChart(container: HTMLElement, options: Record<string, unknown> = {}) {
  const crosshair = new Set<Handler>();
  const range = new Set<Handler>();
  const size = new Set<Handler>();
  const fitContent = vi.fn();
  const view: FakeView = { range: { from: -0.5, to: 19.5 }, spacing: 6, offset: 0, autoScale: true, priceRange: { from: 95, to: 110 } };
  const scrollToPosition = vi.fn((position: number) => {
    view.offset = position;
  });
  const applyTimeScaleOptions = vi.fn((next: { barSpacing?: number }) => {
    if (typeof next.barSpacing === "number") view.spacing = next.barSpacing;
  });
  const setVisibleLogicalRange = vi.fn((range: { from: number; to: number }) => {
    view.range = range;
  });
  const setAutoScale = vi.fn((on: boolean) => {
    view.autoScale = on;
  });
  const setVisibleRange = vi.fn((range: { from: number; to: number }) => {
    view.priceRange = range;
  });
  const timeScale = {
    fitContent,
    getVisibleLogicalRange: () => view.range,
    scrollPosition: () => view.offset,
    scrollToPosition,
    applyOptions: applyTimeScaleOptions,
    setVisibleLogicalRange,
    logicalToCoordinate: (index: number) => index * view.spacing,
    width: () => 800,
    subscribeVisibleLogicalRangeChange: (h: Handler) => range.add(h),
    unsubscribeVisibleLogicalRangeChange: (h: Handler) => range.delete(h),
    subscribeSizeChange: (h: Handler) => size.add(h),
    unsubscribeSizeChange: (h: Handler) => size.delete(h),
  };
  const chart: FakeChart = {
    container,
    options: merge({}, options),
    view,
    scrollToPosition,
    applyTimeScaleOptions,
    setVisibleLogicalRange,
    setAutoScale,
    setVisibleRange,
    fireRange() {
      for (const handler of range) handler(view.range);
    },
    series: [],
    crosshair,
    removed: false,
    fitContent,
    addCustomSeries: vi.fn((view: unknown, seriesOptions: Record<string, unknown>) => {
      const series = makeSeries(view, seriesOptions);
      chart.series.push(series);
      return series;
    }),
    applyOptions: vi.fn((next: Record<string, unknown>) => merge(chart.options, next)),
    resize: vi.fn(),
    remove: vi.fn(() => {
      chart.removed = true;
    }),
    takeScreenshot: vi.fn(() => {
      const canvas = document.createElement("canvas");
      canvas.width = 800;
      canvas.height = 400;
      return canvas;
    }),
    hover(time: number | undefined) {
      const series = chart.series[0];
      const data = new Map();
      const bar = series?.data.find((item) => item.time === time);
      if (series && bar) data.set(series, bar);
      for (const handler of crosshair) handler({ time, point: time === undefined ? undefined : { x: 10, y: 10 }, seriesData: data });
    },
  };
  const api = {
    ...chart,
    subscribeCrosshairMove: (h: Handler) => crosshair.add(h),
    unsubscribeCrosshairMove: (h: Handler) => crosshair.delete(h),
    timeScale: () => timeScale,
    priceScale: () => ({
      width: () => 80,
      options: () => ({ autoScale: view.autoScale }),
      getVisibleRange: () => view.priceRange,
      setAutoScale,
      setVisibleRange,
    }),
  };
  Object.assign(chart, { api });
  charts.push(chart);
  return api;
}

/** Swaps the chart engine for this stand-in, with no charts recorded yet. Each test file loads its own modules. */
export function useFakeCharts() {
  charts.length = 0;
  chartEngine.createChart = createChart as unknown as typeof chartEngine.createChart;
}
