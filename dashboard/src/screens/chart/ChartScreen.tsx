import { useEffect, useRef, useState } from "react";
import { Button, EmptyState, ErrorState, Loading, Pane, Pill, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import type { ScreenProps } from "@/shell/registry";
import { useShell } from "@/shell/useShell";
import { AssetSearch } from "./AssetSearch";
import { CandleChart } from "./CandleChart";
import { sendChartToChat } from "./askFund";
import { renderChartPng } from "./exportPng";
import { formatMoment, formatPercent, formatPrice, safeFileName } from "./format";
import { DEFAULT_TIMEFRAME, TIMEFRAMES } from "./types";
import type { ChartData, Timeframe } from "./types";
import { useChartData } from "./useChartData";

const STORAGE_KEY = "hokma.chart.timeframe";
const NOWRAP = { whiteSpace: "nowrap", flexShrink: 0 } as const;

function savedTimeframe(): Timeframe {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && (TIMEFRAMES as readonly string[]).includes(stored)) return stored as Timeframe;
  } catch {
    /* storage unavailable */
  }
  return DEFAULT_TIMEFRAME;
}

export function ChartScreen({ tabKey, params, active }: ScreenProps) {
  const shell = useShell();
  const { t } = useI18n();
  const [picked, setPicked] = useState<string | null>(null);
  const symbol = params.symbol || picked;

  const { setTabTitle } = shell; // stable; the whole `shell` object changes on every tab update
  useEffect(() => {
    if (!params.symbol && picked) setTabTitle(tabKey, picked);
  }, [params.symbol, picked, setTabTitle, tabKey]);

  if (!symbol) {
    return (
      <Pane as="section" aria-label={t("screen.chart")} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", padding: 24, overflow: "auto" }}>
        <EmptyState icon="chart" title={t("chart.none.title")} description={t("chart.none.text")} />
        <div style={{ width: "min(420px, 100%)" }}>
          <AssetSearch onPick={setPicked} autoFocus />
        </div>
      </Pane>
    );
  }
  return <ChartView key={symbol} symbol={symbol} active={active} />;
}

function ChartView({ symbol, active }: { symbol: string; active: boolean }) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const [timeframe, setTimeframe] = useState<Timeframe>(savedTimeframe);
  const { data, loading, error, refreshFailed, reload } = useChartData(symbol, timeframe, active);
  const [asking, setAsking] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const changeTimeframe = (next: Timeframe) => {
    setTimeframe(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage unavailable */
    }
  };

  const ask = async (snapshot: ChartData) => {
    setAsking(true);
    try {
      const root = rootRef.current?.closest(".hk") ?? document.documentElement;
      const image = await renderChartPng(snapshot, { root, t, locale });
      const opening = snapshot.opening_range;
      const text = opening
        ? t("chart.ask.promptOpening", { symbol: snapshot.symbol, tf: snapshot.timeframe, price: formatPrice(opening.high, locale) })
        : t("chart.ask.prompt", { symbol: snapshot.symbol, tf: snapshot.timeframe });
      const conversation = await sendChartToChat(image, safeFileName(snapshot.symbol, snapshot.timeframe), text);
      shell.openScreen("chat", { conversation });
    } catch {
      shell.toast({ tone: "bad", text: t("chart.ask.failed") });
    } finally {
      setAsking(false);
    }
  };

  const change = data?.change_pct ?? null;
  const direction = change === null ? 0 : change > 0 ? 1 : change < 0 ? -1 : 0;
  const priceColor = direction < 0 ? "var(--down)" : "var(--up)";
  const lastMoment = data ? (data.last_trade_at ?? data.candles[data.candles.length - 1]?.t ?? data.fetched_at) : 0;
  const ready = data !== null && data.candles.length > 0;

  return (
    <Pane as="section" aria-label={`${symbol} ${timeframe}`} style={{ flex: 1, display: "flex", flexDirection: "column" }}>
      <div ref={rootRef} style={{ display: "contents" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid var(--border)", flexWrap: "nowrap", minWidth: 0 }}>
          <span className="h2 mono" data-testid="chart-title" style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
            {symbol} · {timeframe}
          </span>
          {data ? (
            <span className="p" style={{ fontSize: 12, minWidth: 0, flex: "0 1 auto", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {data.name}
              {data.exchange ? ` · ${data.exchange}` : ""}
            </span>
          ) : null}
          {data ? (
            <>
              <span className="mono" data-testid="chart-price" style={{ color: priceColor, whiteSpace: "nowrap", flexShrink: 0 }}>
                {formatPrice(data.last_price, locale)}
              </span>
              <Pill tone="acc" className={direction < 0 ? "bad" : undefined} style={NOWRAP} data-testid="chart-change">
                {formatPercent(data.change_pct, locale)}
              </Pill>
              {data.market_open ? (
                <Pill tone="ok" style={NOWRAP} data-testid="market-state">
                  {t("chart.market.open")}
                </Pill>
              ) : (
                <Pill style={NOWRAP} data-testid="market-state" title={t("chart.lastData", { when: formatMoment(lastMoment, data.timezone, locale) })}>
                  {t("chart.market.closed")} · {t("chart.lastData", { when: formatMoment(lastMoment, data.timezone, locale) })}
                </Pill>
              )}
              {data.delayed && data.data_age_seconds !== null ? (
                <Pill tone="warn" style={NOWRAP} data-testid="delay-notice">
                  {t("chart.delayed", { minutes: Math.max(1, Math.round(data.data_age_seconds / 60)) })}
                </Pill>
              ) : null}
              {data.stale ? (
                <Pill tone="warn" style={NOWRAP} data-testid="stale-notice">
                  {t("chart.stale", { when: formatMoment(data.fetched_at, data.timezone, locale) })}
                </Pill>
              ) : null}
              {refreshFailed ? (
                <Pill tone="warn" style={NOWRAP} data-testid="refresh-failed">
                  {t("chart.refreshFailed")}
                </Pill>
              ) : null}
            </>
          ) : null}
          <div style={{ marginLeft: "auto", flexShrink: 0 }}>
            <Segmented
              label={t("chart.timeframe")}
              value={timeframe}
              onChange={changeTimeframe}
              options={TIMEFRAMES.map((tf) => ({ value: tf, label: tf }))}
            />
          </div>
        </div>

        {loading && !data ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Loading label={t("chart.loading")} />
          </div>
        ) : error && !data ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <ErrorState title={t("chart.error.title")} message={error} onRetry={reload} />
          </div>
        ) : data && !ready ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <EmptyState icon="chart" title={t("chart.empty.title")} description={t("chart.empty.text", { symbol, tf: timeframe })} />
          </div>
        ) : data ? (
          <CandleChart data={data} />
        ) : null}

        <div style={{ display: "flex", gap: 8, padding: "10px 14px", borderTop: "1px solid var(--border)", flexWrap: "wrap", alignItems: "center" }}>
          <span className="p" style={{ fontSize: 12 }}>
            {t("chart.source", { source: data?.source ?? "Yahoo Finance" })}
          </span>
          <Button size="sm" style={{ marginLeft: "auto" }} onClick={reload} disabled={loading}>
            {t("chart.refresh")}
          </Button>
          <Button size="sm" variant="primary" disabled={!ready || asking} onClick={() => data && void ask(data)}>
            {asking ? t("chart.ask.sending") : t("chart.ask")}
          </Button>
        </div>
      </div>
    </Pane>
  );
}
