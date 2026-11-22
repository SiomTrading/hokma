import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { PriceChart } from "./PriceChart";
import type { PriceBar } from "./PriceChart";
import { useFakeCharts } from "./testChartLib";

const T0 = 1793802600; // 09:30 New York
const BARS: PriceBar[] = Array.from({ length: 60 }, (_, i) => ({ t: T0 - 1800 + i * 60, o: 1, h: 2, l: 0.5, c: 1.5, v: 1 }));
const SESSIONS = [
  { name: "preMarket", start: T0 - 7200, end: T0 },
  { name: "regularMarket", start: T0, end: T0 + 23400 },
  { name: "overnightMarket", start: T0 + 23400, end: T0 + 40000 },
];

beforeEach(useFakeCharts);

describe("the shading by pieces of the day (SPEC-016 3.4)", () => {
  it("draws one band per piece the provider named, with its name, and clips the ones that run past the bars", () => {
    render(<PriceChart bars={BARS} timeframe="1m" timezone="America/New_York" sessions={SESSIONS} label="QQQ 1m" />);
    const layer = screen.getByTestId("session-bands");
    expect(layer.children.length).toBeGreaterThan(0);
    const names = [...layer.children].map((el) => el.getAttribute("data-session"));
    expect(names).toContain("preMarket");
    expect(names).toContain("regularMarket");
    expect(names).not.toContain("overnightMarket"); // it starts after the last bar: nothing to paint
    const first = layer.children[0] as HTMLElement;
    expect(first.style.left).toBe("0px");           // the first piece starts before the first bar
  });

  it("draws nothing when the provider gave no pieces, and none on a range chart", () => {
    const { rerender } = render(<PriceChart bars={BARS} timeframe="1m" timezone="America/New_York" sessions={null} label="QQQ 1m" />);
    expect(screen.queryByTestId("session-bands")).not.toBeInTheDocument();
    rerender(<PriceChart bars={BARS} axis="index" timeframe="1m" timezone="America/New_York" sessions={SESSIONS} label="QQQ 1m" />);
    expect(screen.queryByTestId("session-bands")).not.toBeInTheDocument();
  });
});
