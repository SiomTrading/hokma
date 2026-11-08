/**
 * Reading and applying the view of the price chart (zoom, scroll, price scale) through the chart library. The state it
 * reads and writes is `ChartViewState` of `chartState.ts`. Bars are located by their real time (`t`), so a saved view
 * still makes sense when the bars loaded now are not the ones that were loaded when it was saved.
 */
import type { IChartApi, Logical } from "lightweight-charts";
import type { ChartViewState, PriceScaleState } from "./chartState";

interface TimedBar {
  t: number;
}

/** Half a bar from the right edge still counts as "at the edge". */
export const FOLLOW_TOLERANCE = 0.5;
const FALLBACK_SPACING = 6;

const clamp = (value: number, low: number, high: number) => Math.min(Math.max(value, low), high);

/**
 * Index of the bar a real time points to: `first` is the first bar at or after `time`, `last` the last bar at or before
 * it. A time outside the bars gives the nearest end.
 */
export function barIndexAt(bars: readonly TimedBar[], time: number, side: "first" | "last"): number {
  if (bars.length === 0) return 0;
  let low = 0;
  let high = bars.length - 1;
  if (side === "first") {
    while (low < high) {
      const mid = (low + high) >> 1;
      if (bars[mid]!.t < time) low = mid + 1;
      else high = mid;
    }
    return low;
  }
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (bars[mid]!.t > time) high = mid - 1;
    else low = mid;
  }
  return low;
}

export interface ViewRead {
  view: ChartViewState;
  /** The prices on the vertical axis right now, auto or not (for the tests and for support). */
  priceRange: { min: number; max: number } | null;
}

function spacingOf(chart: IChartApi, range: { from: number; to: number }): number {
  const scale = chart.timeScale();
  const a = scale.logicalToCoordinate(0 as Logical);
  const b = scale.logicalToCoordinate(1 as Logical);
  if (a !== null && b !== null && Number.isFinite(b - a) && b - a > 0) return b - a;
  const width = scale.width();
  const bars = range.to - range.from;
  return width > 0 && bars > 0 ? width / bars : FALLBACK_SPACING;
}

/** The view the chart has now, or null when there is nothing to read yet (no bars, or the chart has no layout). */
export function viewFromChart(chart: IChartApi, bars: readonly TimedBar[]): ViewRead | null {
  if (bars.length === 0) return null;
  const scale = chart.timeScale();
  const range = scale.getVisibleLogicalRange();
  if (!range) return null;
  const last = bars.length - 1;
  const first = clamp(Math.ceil(range.from), 0, last);
  const end = clamp(Math.floor(range.to), first, last);
  const rightOffset = scale.scrollPosition();
  const axis = chart.priceScale("right");
  const visible = axis.getVisibleRange();
  const priceRange = visible ? { min: Math.min(visible.from, visible.to), max: Math.max(visible.from, visible.to) } : null;
  const auto = axis.options().autoScale !== false;
  const price: PriceScaleState = auto || !priceRange || !(priceRange.min < priceRange.max) ? { auto: true } : { auto: false, min: priceRange.min, max: priceRange.max };
  return {
    view: {
      follow: rightOffset >= -FOLLOW_TOLERANCE,
      barSpacing: Math.round(spacingOf(chart, range) * 1000) / 1000,
      rightOffset: Math.round(rightOffset * 1000) / 1000,
      from: bars[first]!.t,
      to: bars[end]!.t,
      price,
    },
    priceRange,
  };
}

/** The same view, ignoring noise below what a student could see. */
export function sameView(a: ChartViewState, b: ChartViewState): boolean {
  if (a.follow !== b.follow || a.from !== b.from || a.to !== b.to) return false;
  if (Math.abs(a.barSpacing - b.barSpacing) > 0.01 || Math.abs(a.rightOffset - b.rightOffset) > 0.01) return false;
  if (a.price.auto !== b.price.auto) return false;
  if (!a.price.auto && !b.price.auto) {
    const span = Math.abs(a.price.max - a.price.min) || 1;
    return Math.abs(a.price.min - b.price.min) < span * 1e-6 && Math.abs(a.price.max - b.price.max) < span * 1e-6;
  }
  return true;
}

/**
 * Puts the chart back in a saved view. Following the right edge: spacing and the offset from the last bar, so bars that
 * arrive later keep entering by the right. Otherwise: the same spacing with the bar of the saved right end at the right
 * edge, found by real time. A saved start older than the oldest bar loaded makes the view start at that bar (older
 * history is never asked for). The price scale goes back to automatic or to the saved range.
 */
export function restoreView(chart: IChartApi, view: ChartViewState, bars: readonly TimedBar[]): void {
  if (bars.length === 0) return;
  const scale = chart.timeScale();
  const last = bars.length - 1;
  scale.applyOptions({ barSpacing: view.barSpacing });
  if (view.follow) {
    scale.scrollToPosition(view.rightOffset, false);
  } else {
    const end = barIndexAt(bars, view.to, "last");
    scale.scrollToPosition(end - last, false);
    if (view.from < bars[0]!.t) scale.setVisibleLogicalRange({ from: -0.5, to: end + 0.5 });
  }
  const axis = chart.priceScale("right");
  if (view.price.auto) {
    if (axis.options().autoScale === false) axis.setAutoScale(true);
  } else {
    axis.setAutoScale(false);
    axis.setVisibleRange({ from: view.price.min, to: view.price.max });
  }
}
