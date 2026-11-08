import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { barIndexAt, sameView } from "./chartView";
import type { ChartViewState } from "./chartState";
import { PriceChart } from "./PriceChart";
import type { PriceBar } from "./PriceChart";
import { charts, useFakeCharts } from "./testChartLib";

const T0 = 1793802600;
const STEP = 300;
const barsOf = (count: number, start = T0): PriceBar[] =>
  Array.from({ length: count }, (_, i) => ({ t: start + i * STEP, o: 100 + i, h: 102 + i, l: 99 + i, c: 101 + i, v: 10 }));

beforeEach(useFakeCharts); // jsdom has no canvas: the chart instance is a recording stand-in

function mount(props: { bars?: PriceBar[]; initialView?: ChartViewState | null; onViewChange?: (view: ChartViewState) => void } = {}) {
  const bars = props.bars ?? barsOf(100);
  const ui = (list: PriceBar[]) => (
    <div className="hk">
      <PriceChart bars={list} timeframe="5m" timezone="America/New_York" label="NQ=F 5m" initialView={props.initialView ?? null} onViewChange={props.onViewChange} />
    </div>
  );
  const view = render(ui(bars));
  return { ...view, again: (list: PriceBar[]) => view.rerender(ui(list)), chart: () => charts.at(-1)! };
}

const FOLLOW: ChartViewState = { follow: true, barSpacing: 9.5, rightOffset: 3, from: T0 + 60 * STEP, to: T0 + 99 * STEP, price: { auto: true } };
const BACK: ChartViewState = { follow: false, barSpacing: 4.25, rightOffset: -30, from: T0 + 20 * STEP, to: T0 + 70 * STEP, price: { auto: true } };

describe("first snapshot", () => {
  it("without a saved view fits all bars, reports nothing and mirrors the live view in the DOM", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    expect(chart().fitContent).toHaveBeenCalledTimes(1);
    expect(chart().scrollToPosition).not.toHaveBeenCalled();
    expect(onViewChange).not.toHaveBeenCalled();
    const live = JSON.parse(screen.getByTestId("chart-body").getAttribute("data-view")!);
    expect(live).toMatchObject({ follow: true, barSpacing: 6, price: { auto: true }, priceRange: { min: 95, max: 110 } });
  });

  it("a saved view that follows the edge restores spacing and offset from the last bar, with no fit and no report", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ initialView: FOLLOW, onViewChange });
    expect(chart().fitContent).not.toHaveBeenCalled();
    expect(chart().applyTimeScaleOptions).toHaveBeenCalledWith({ barSpacing: 9.5 });
    expect(chart().scrollToPosition).toHaveBeenCalledWith(3, false);
    expect(chart().view.spacing).toBe(9.5);
    expect(onViewChange).not.toHaveBeenCalled();
  });

  it("a saved view scrolled back is found by real time: the bar of the saved right end goes to the right edge", () => {
    const { chart } = mount({ initialView: BACK });
    expect(chart().fitContent).not.toHaveBeenCalled();
    expect(chart().applyTimeScaleOptions).toHaveBeenCalledWith({ barSpacing: 4.25 });
    expect(chart().scrollToPosition).toHaveBeenCalledWith(70 - 99, false);
    expect(chart().setVisibleLogicalRange).not.toHaveBeenCalled();
  });

  it("finds the right end by time even when the bars loaded now are not the same ones", () => {
    // 40 new bars came in since it was saved: the same moment is 40 bars further from the right edge.
    const { chart } = mount({ initialView: BACK, bars: barsOf(140) });
    expect(chart().scrollToPosition).toHaveBeenCalledWith(70 - 139, false);
  });

  it("a saved start older than the oldest bar loaded starts the view at that bar", () => {
    const { chart } = mount({ initialView: { ...BACK, from: T0 - 500 * STEP } });
    expect(chart().setVisibleLogicalRange).toHaveBeenCalledWith({ from: -0.5, to: 70.5 });
  });

  it("a manual price scale is put back; an automatic one is left alone", () => {
    const manual = mount({ initialView: { ...FOLLOW, price: { auto: false, min: 101.5, max: 140 } } });
    expect(manual.chart().setAutoScale).toHaveBeenCalledWith(false);
    expect(manual.chart().setVisibleRange).toHaveBeenCalledWith({ from: 101.5, to: 140 });
    manual.unmount();
    const auto = mount({ initialView: FOLLOW });
    expect(auto.chart().setAutoScale).not.toHaveBeenCalled();
    expect(auto.chart().setVisibleRange).not.toHaveBeenCalled();
  });
});

describe("a snapshot that is not the first (reconnection)", () => {
  it("keeps the view the student has: no fit, and the view is put back after setData", () => {
    const { chart, again } = mount({ initialView: BACK });
    chart().view.offset = -12; // the student moved after the restore
    chart().view.spacing = 7;
    again(barsOf(101)); // same bars plus one: a new snapshot of the same chart is a live change, so force a real snapshot below
    again(barsOf(100, T0 + 10 * STEP)); // the engine sent a different window: this one is a snapshot
    expect(chart().series[0]!.setData).toHaveBeenCalledTimes(2);
    expect(chart().fitContent).not.toHaveBeenCalled();
    expect(chart().applyTimeScaleOptions).toHaveBeenLastCalledWith({ barSpacing: 7 });
  });

  it("does not use the saved view again", () => {
    const { chart, again } = mount({ initialView: FOLLOW });
    const calls = chart().scrollToPosition.mock.calls.length;
    again(barsOf(100, T0 + 10 * STEP));
    expect(chart().scrollToPosition.mock.calls.length).toBe(calls + 1); // the view it had, not the saved one
    expect(chart().scrollToPosition).toHaveBeenLastCalledWith(chart().view.offset, false);
  });

  it("a live bar does not touch the view", () => {
    const { chart, again } = mount({ initialView: BACK });
    const scrolls = chart().scrollToPosition.mock.calls.length;
    again(barsOf(101));
    expect(chart().series[0]!.update).toHaveBeenCalledTimes(1);
    expect(chart().series[0]!.setData).toHaveBeenCalledTimes(1);
    expect(chart().scrollToPosition.mock.calls.length).toBe(scrolls);
  });
});

describe("what the student moves is reported", () => {
  it("the wheel reports the zoom with real times, spacing, follow and price", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    chart().view.range = { from: 40.2, to: 79.8 };
    chart().view.spacing = 12.5;
    chart().view.offset = -20;
    fireEvent.wheel(screen.getByTestId("chart-body"));
    expect(onViewChange).toHaveBeenCalledTimes(1);
    expect(onViewChange).toHaveBeenCalledWith({
      follow: false,
      barSpacing: 12.5,
      rightOffset: -20,
      from: T0 + 41 * STEP,
      to: T0 + 79 * STEP,
      price: { auto: true },
    });
  });

  it("at the right edge (within half a bar) it reports follow", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    chart().view.offset = -0.4;
    chart().view.spacing = 8;
    fireEvent.wheel(screen.getByTestId("chart-body"));
    expect(onViewChange.mock.calls[0]![0]).toMatchObject({ follow: true, rightOffset: -0.4 });
    chart().view.offset = -0.6;
    fireEvent.wheel(screen.getByTestId("chart-body"));
    expect(onViewChange.mock.calls[1]![0]).toMatchObject({ follow: false });
  });

  it("a dragged price scale reports its range; the double click that makes it automatic reports that", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    chart().view.autoScale = false;
    chart().view.priceRange = { from: 120, to: 150 };
    fireEvent.pointerDown(screen.getByTestId("chart-body"));
    fireEvent.pointerUp(screen.getByTestId("chart-body"));
    expect(onViewChange.mock.calls[0]![0].price).toEqual({ auto: false, min: 120, max: 150 });
    chart().view.autoScale = true;
    fireEvent.doubleClick(screen.getByTestId("chart-body"));
    expect(onViewChange.mock.calls[1]![0].price).toEqual({ auto: true });
  });

  it("a click that moved nothing reports nothing", () => {
    const onViewChange = vi.fn();
    mount({ onViewChange });
    fireEvent.pointerDown(screen.getByTestId("chart-body"));
    fireEvent.pointerUp(screen.getByTestId("chart-body"));
    fireEvent.doubleClick(screen.getByTestId("chart-body"));
    expect(onViewChange).not.toHaveBeenCalled();
  });

  it("a drag reports while it goes on, and a range change nobody asked for (a live bar) reports nothing", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    chart().view.range = { from: 10, to: 50 };
    chart().fireRange(); // not the student: nothing
    expect(onViewChange).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByTestId("chart-body"));
    chart().view.range = { from: 12, to: 52 };
    chart().view.offset = -47;
    chart().fireRange();
    expect(onViewChange).toHaveBeenCalledTimes(1);
    expect(onViewChange.mock.calls[0]![0]).toMatchObject({ from: T0 + 12 * STEP, to: T0 + 52 * STEP, follow: false });
  });

  it("the restore itself, a fit and a resize are not reports", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ initialView: BACK, onViewChange });
    chart().fireRange();
    expect(onViewChange).not.toHaveBeenCalled();
  });
});

describe("helpers", () => {
  const bars = [{ t: 10 }, { t: 20 }, { t: 30 }, { t: 40 }];
  it("finds the bar a time points to", () => {
    expect(barIndexAt(bars, 20, "first")).toBe(1);
    expect(barIndexAt(bars, 25, "first")).toBe(2);
    expect(barIndexAt(bars, 25, "last")).toBe(1);
    expect(barIndexAt(bars, 5, "last")).toBe(0);
    expect(barIndexAt(bars, 99, "first")).toBe(3);
    expect(barIndexAt([], 5, "first")).toBe(0);
  });

  it("two views are the same below what a student can see", () => {
    expect(sameView(FOLLOW, { ...FOLLOW, barSpacing: 9.505 })).toBe(true);
    expect(sameView(FOLLOW, { ...FOLLOW, barSpacing: 9.6 })).toBe(false);
    expect(sameView(FOLLOW, { ...FOLLOW, to: FOLLOW.to + 1 })).toBe(false);
    expect(sameView(FOLLOW, { ...FOLLOW, price: { auto: false, min: 1, max: 2 } })).toBe(false);
  });
});


describe("a chart that mounts hidden", () => {
  it("places the saved view when it gets a width, not before (spacing and fit mean nothing at width 0)", () => {
    const observers: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          observers.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (run: () => void) => {
      run();
      return 0;
    });
    try {
      const { chart } = mount({ initialView: BACK });
      expect(chart().applyTimeScaleOptions).not.toHaveBeenCalled(); // no width yet: nothing placed
      expect(chart().fitContent).not.toHaveBeenCalled();
      const host = screen.getByTestId("price-chart");
      Object.defineProperty(host, "clientWidth", { configurable: true, value: 900 });
      Object.defineProperty(host, "clientHeight", { configurable: true, value: 400 });
      for (const run of observers) run();
      expect(chart().applyTimeScaleOptions).toHaveBeenCalledWith({ barSpacing: 4.25 });
      expect(chart().scrollToPosition).toHaveBeenCalledWith(70 - 99, false);
      // and only once
      for (const run of observers) run();
      expect(chart().scrollToPosition).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("fits the chart when it gets a width if there is no saved view", () => {
    const observers: Array<() => void> = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          observers.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (run: () => void) => {
      run();
      return 0;
    });
    try {
      const { chart } = mount();
      expect(chart().fitContent).not.toHaveBeenCalled();
      Object.defineProperty(screen.getByTestId("price-chart"), "clientWidth", { configurable: true, value: 900 });
      for (const run of observers) run();
      expect(chart().fitContent).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("when the page goes away", () => {
  it("hands over the view as the chart has it at that moment, even if no event reported the last movement", () => {
    const onViewChange = vi.fn();
    const { chart } = mount({ onViewChange });
    chart().view.spacing = 14;
    chart().view.offset = -9;
    window.dispatchEvent(new Event("pagehide"));
    expect(onViewChange).toHaveBeenCalledTimes(1);
    expect(onViewChange.mock.calls[0]![0]).toMatchObject({ barSpacing: 14, rightOffset: -9, follow: false });
  });

  it("says nothing when the view is the one the chart was left in", () => {
    const onViewChange = vi.fn();
    mount({ onViewChange });
    window.dispatchEvent(new Event("pagehide"));
    expect(onViewChange).not.toHaveBeenCalled();
  });

  it("stops listening when the chart goes away", () => {
    const onViewChange = vi.fn();
    const { chart, unmount } = mount({ onViewChange });
    chart().view.spacing = 14;
    unmount();
    window.dispatchEvent(new Event("pagehide"));
    expect(onViewChange).not.toHaveBeenCalled();
  });
});
