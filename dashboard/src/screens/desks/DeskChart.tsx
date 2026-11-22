import { useState } from "react";
import { Button, EmptyState, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { BrokerAlertBand } from "../chart/BrokerAlertBand";
import { IntervalBar } from "../chart/IntervalBar";
import { PriceChart } from "../chart/PriceChart";
import { errorText, secondsDisabledReason, sourceLine, ticksDisabledReason } from "../chart/liveState";
import { chartTitle } from "../chart/intervals";
import { loadInterval } from "../chart/favorites";
import type { Timeframe } from "../chart/types";
import { useLiveChart } from "../chart/useLiveChart";
import { loadAssetInterval, saveAssetInterval } from "./terminalPrefs";

/** The chart of the asset in focus: the same live chart of the Chart screen, with its own interval box. */
export function DeskChart({ symbol, deskId }: { symbol: string; deskId?: number }) {
  const { t } = useI18n();
  const [chosen, setChosen] = useState<{ symbol: string; timeframe: Timeframe } | null>(null);
  const [boxOpen, setBoxOpen] = useState(false);
  // Each asset remembers its own interval; one that was never set opens with the chart default.
  const timeframe = chosen?.symbol === symbol ? chosen.timeframe : (loadAssetInterval(symbol) ?? loadInterval());
  const setTimeframe = (next: Timeframe) => {
    saveAssetInterval(symbol, next);
    setChosen({ symbol, timeframe: next });
  };
  const { live, data, retry } = useLiveChart(symbol, timeframe, undefined, deskId);
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
        sessions={live.sessions}
        label={t("chart.aria", { symbol: data.symbol, tf: data.timeframe })}
      />
    );
  } else {
    body = null;
  }

  return (
    <Pane className="dterm-chart" aria-label={t("desks.terminal.chart", { symbol })}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{chartTitle(symbol, timeframe, t)}</span>
        <span className="dterm-ph-end">
          <IntervalBar
            value={timeframe}
            onChange={setTimeframe}
            open={boxOpen}
            onOpenChange={setBoxOpen}
            secondsDisabledReason={secondsDisabledReason(live, t)}
            ticksDisabledReason={ticksDisabledReason(live, t)}
            boxStyle={{ top: "calc(100% + 4px)", right: 12 }}
          />
        </span>
      </div>
      <BrokerAlertBand alert={live.brokerAlert} assetClass={live.assetClass} />
      <div className="dterm-chart-body">{body}</div>
      {live.source ? <div className="dterm-foot hint">{sourceLine(live, t)}</div> : null}
    </Pane>
  );
}
