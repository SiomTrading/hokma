import { createRef } from "react";
import { createRoot } from "react-dom/client";
import "../../src/index.css";
import { registerMessages, setLocale } from "../../src/i18n";
import chartMessages from "../../src/i18n/messages/chart";
import { Pane } from "../../src/design";
import { PriceChart } from "../../src/screens/chart/PriceChart";
import type { PriceChartHandle } from "../../src/screens/chart/PriceChart";
import { data } from "./fixture";

registerMessages(chartMessages);
setLocale("en");

/** The capture script asks the chart for its own screenshot through this handle (what the PNG export uses). */
const chart = createRef<PriceChartHandle>();
(window as unknown as { priceChart: typeof chart }).priceChart = chart;

/** The chart alone, at a fixed size in the dark theme, with the bars of `fixture.ts`. */
createRoot(document.getElementById("root")!).render(
  <div className="hk">
    <div style={{ width: 1200, height: 520, display: "flex", padding: 20, boxSizing: "border-box" }}>
      <Pane style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
        <PriceChart
          ref={chart}
          bars={data.candles}
          timeframe={data.timeframe}
          timezone={data.timezone}
          opening={data.opening_range}
          label={`${data.symbol} ${data.timeframe}`}
        />
      </Pane>
    </div>
  </div>,
);
