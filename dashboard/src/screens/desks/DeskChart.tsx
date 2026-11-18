import { useState } from "react";
import { Button, EmptyState, Loading, Pane, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { PriceChart } from "../chart/PriceChart";
import { errorText } from "../chart/liveState";
import { DEFAULT_TIMEFRAME, TIMEFRAMES } from "../chart/types";
import type { Timeframe } from "../chart/types";
import { useLiveChart } from "../chart/useLiveChart";

/** The chart of the asset in focus: the same live chart of the Chart screen, with its own interval box. */
export function DeskChart({ symbol }: { symbol: string }) {
  const { t } = useI18n();
  const [timeframe, setTimeframe] = useState<Timeframe>(DEFAULT_TIMEFRAME);
  const { live, data, retry } = useLiveChart(symbol, timeframe);
  const error = live.error;
  const ready = !!data && data.candles.length > 0;

  let body;
  if (error) {
    body = (
      <div className="live-error" role="alert" data-testid="desk-chart-error" data-code={error.code}>
        <span className="h2">{t("chart.error.title")}</span>
        <p className="p">{errorText(error, t)}</p>
        <Button size="sm" onClick={retry}>
          {t("ui.error.retry")}
        </Button>
      </div>
    );
  } else if (!live.ready) {
    body = (
      <div className="dterm-center">
        <Loading label={t("chart.loading")} />
      </div>
    );
  } else if (data && !ready) {
    body = (
      <div className="dterm-center">
        <EmptyState icon="chart" title={t("chart.empty.title")} description={t("chart.empty.text", { symbol, tf: timeframe })} />
      </div>
    );
  } else if (data) {
    body = (
      <PriceChart
        key={symbol + "|" + timeframe}
        bars={data.candles}
        axis={data.interval?.kind === "range" ? "index" : "time"}
        timeframe={data.timeframe}
        timezone={data.timezone}
        opening={data.opening_range}
        label={t("chart.aria", { symbol: data.symbol, tf: data.timeframe })}
      />
    );
  } else {
    body = null;
  }

  return (
    <Pane className="dterm-chart" aria-label={t("desks.terminal.chart", { symbol })}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{`${symbol} · ${timeframe}`}</span>
        <span className="dterm-ph-end">
          <Segmented<Timeframe>
            label={t("chart.timeframe")}
            value={timeframe}
            onChange={setTimeframe}
            options={TIMEFRAMES.map((tf) => ({ value: tf, label: tf }))}
          />
        </span>
      </div>
      <div className="dterm-chart-body">{body}</div>
      {live.source ? <div className="dterm-foot hint">{t("chart.source", { source: live.source })}</div> : null}
    </Pane>
  );
}
