import { act, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { PriceChart } from "./PriceChart";
import type { PriceBar, PriceChartHandle } from "./PriceChart";
import { charts, useFakeCharts } from "./testChartLib";

const NINE_THIRTY = 1793802600; // 2026-11-04 09:30 New York

/** Three range bars that share one second, then one more: the axis runs by index, the labels by real time. */
const RANGE: PriceBar[] = [
  { t: NINE_THIRTY, o: 100, h: 102.5, l: 100, c: 102.5, v: 3, i: 0, approx: true },
  { t: NINE_THIRTY, o: 102.75, h: 105.25, l: 102.75, c: 105.25, v: 2, i: 1, approx: true },
  { t: NINE_THIRTY, o: 105.5, h: 108, l: 105.5, c: 108, v: 4, i: 2 },
  { t: NINE_THIRTY + 600, o: 107.75, h: 107.75, l: 105.25, c: 105.25, v: 1, i: 3 },
];

function themed() {
  const root = document.createElement("div");
  root.className = "hk";
  for (const [name, value] of Object.entries({ "--up": "rgb(0, 204, 204)", "--down": "rgb(242, 96, 56)", "--grid": "rgba(1, 1, 1, 0.05)", "--border": "rgb(2, 2, 2)", "--t3": "rgb(3, 3, 3)", "--org": "rgb(4, 4, 4)" })) {
    root.style.setProperty(name, value);
  }
  document.body.appendChild(root);
  return root;
}

beforeEach(useFakeCharts); // jsdom has no canvas: the chart instance is a recording stand-in

describe("PriceChart by index (range bars)", () => {
  it("hands the series the index as time, keeps the approx flag and labels the axis with the real time", () => {
    const root = themed();
    render(<PriceChart bars={RANGE} axis="index" timeframe="r:10t" timezone="America/New_York" label="NQ=F Range 10t" />, { container: root });
    const chart = charts.at(-1)!;
    const series = chart.series[0]!;
    expect(series.setData).toHaveBeenCalledTimes(1);
    expect(series.data.map((item) => item.time)).toEqual([0, 1, 2, 3]);
    expect(series.data.map((item) => item.approx)).toEqual([true, true, false, false]);
    const options = chart.options as { timeScale: { tickMarkFormatter: (time: number) => string | null }; localization: { timeFormatter: (time: number) => string } };
    expect(options.timeScale.tickMarkFormatter(1)).toBe("09:30");
    expect(options.timeScale.tickMarkFormatter(3)).toBe("09:40");
    expect(options.localization.timeFormatter(3)).toBe("09:40");
    expect(options.timeScale.tickMarkFormatter(99)).toBeNull();
    root.remove();
  });

  it("a new range bar goes through update with its index", () => {
    const root = themed();
    const view = render(<PriceChart bars={RANGE} axis="index" timeframe="r:10t" timezone="America/New_York" label="x" />, { container: root });
    const next = [...RANGE, { t: NINE_THIRTY + 610, o: 105, h: 105, l: 102.5, c: 102.5, v: 1, i: 4 }];
    view.rerender(<PriceChart bars={next} axis="index" timeframe="r:10t" timezone="America/New_York" label="x" />);
    const series = charts.at(-1)!.series[0]!;
    expect(series.setData).toHaveBeenCalledTimes(1);
    expect(series.update).toHaveBeenCalledWith(expect.objectContaining({ time: 4, close: 102.5 }));
    root.remove();
  });

  it("the readout of a range bar shows its real time", () => {
    const root = themed();
    render(<PriceChart bars={RANGE} axis="index" timeframe="r:10t" timezone="America/New_York" label="x" />, { container: root });
    act(() => charts.at(-1)!.hover(3));
    expect(screen.getByTestId("chart-readout")).toHaveTextContent("09:40");
    root.remove();
  });
});

describe("PriceChart colors and handle", () => {
  it("candles, grid, axes and crosshair take the theme tokens, and follow a theme switch", () => {
    const root = themed();
    render(<PriceChart bars={RANGE} timeframe="5m" timezone="UTC" label="x" />, { container: root });
    const chart = charts.at(-1)!;
    const series = chart.series[0]!;
    expect(series.options).toMatchObject({ upColor: "rgb(0, 204, 204)", downColor: "rgb(242, 96, 56)" });
    expect(chart.options).toMatchObject({
      layout: { textColor: "rgb(3, 3, 3)" },
      grid: { horzLines: { color: "rgba(1, 1, 1, 0.05)" } },
      rightPriceScale: { borderColor: "rgb(2, 2, 2)" },
      crosshair: { vertLine: { color: "rgb(3, 3, 3)" } },
    });
    root.style.setProperty("--up", "rgb(0, 122, 122)");
    return new Promise<void>((done) => {
      root.classList.add("light");
      setTimeout(() => {
        expect(series.applyOptions).toHaveBeenCalledWith(expect.objectContaining({ upColor: "rgb(0, 122, 122)" }));
        root.remove();
        done();
      }, 0);
    });
  });

  it("the handle gives the library's screenshot, prices as y and the price axis width", () => {
    const root = themed();
    const ref = createRef<PriceChartHandle>();
    render(<PriceChart ref={ref} bars={RANGE} timeframe="5m" timezone="UTC" label="x" />, { container: root });
    const shot = ref.current!.takeScreenshot();
    expect(charts.at(-1)!.takeScreenshot).toHaveBeenCalledTimes(1);
    expect(shot.width).toBe(800);
    expect(ref.current!.priceToCoordinate(100)).toBe(900);
    expect(ref.current!.priceAxisWidth()).toBe(80);
    root.remove();
  });

  it("removes the chart when it goes away", () => {
    const root = themed();
    const view = render(<PriceChart bars={RANGE} timeframe="5m" timezone="UTC" label="x" />, { container: root });
    view.unmount();
    expect(charts.at(-1)!.remove).toHaveBeenCalledTimes(1);
    root.remove();
  });
});
