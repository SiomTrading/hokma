import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { Button, Dot, EmptyState, Loading, Pane, Pill, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { connectionsHref, requestFocus } from "@/screens/connections/focus";
import type { ScreenProps } from "@/shell/registry";
import { tabKeyFor } from "@/shell/store";
import { useShell } from "@/shell/useShell";
import { AssetSearch, AssetSearchPopover } from "./AssetSearch";
import { sendChartToChat } from "./askFund";
import { renderChartPng } from "./exportPng";
import { formatMoment, formatPercent, formatPrice, safeFileName } from "./format";
import { loadFavorites, saveInterval, toggleFavorite } from "./favorites";
import { activateTab, cancelPending, defaultInterval, dropClosed, dropTab, flushPending, moveTab, queueView, readLast, readTab, saveTabInterval, seedTab } from "./chartState";
import type { ChartViewState } from "./chartState";
import { IntervalBox } from "./IntervalBox";
import { intervalLabel } from "./intervals";
import { PriceChart } from "./PriceChart";
import type { PriceChartHandle } from "./PriceChart";
import "./live.css";
import { barMode, delayMinutes, errorText, historyDelayMinutes, roundedRange, secondsDisabledReason, ticksDisabledReason } from "./liveState";
import type { ChartNotice } from "./liveState";
import type { ChartData, Timeframe } from "./types";
import { useChartShortcuts } from "./useChartShortcuts";
import { useLiveChart } from "./useLiveChart";

const NOWRAP = { whiteSpace: "nowrap", flexShrink: 0 } as const;
/** Error codes that are fixed in Connections: the chart says so and links there. */
const CONNECTION_CODES = new Set(["market.limit", "market.not_entitled", "market.seconds_need_ticks"]);

export function ChartScreen({ tabKey, params, active }: ScreenProps) {
  const shell = useShell();
  const { t } = useI18n();
  const [picked, setPicked] = useState<string | null>(null);
  const symbol = picked || params.symbol || null;

  const { setTabTitle, setTabParams } = shell; // stable; the whole `shell` object changes on every tab update
  /** The symbol belongs to the tab: its key follows it. Without a tab to change (a bare screen) it is kept here. */
  const tabsRef = useRef(shell.tabs);
  tabsRef.current = shell.tabs;
  const changeSymbol = useCallback(
    (next: string) => {
      const nextKey = tabKeyFor("chart", { symbol: next });
      const merged = nextKey !== tabKey && tabsRef.current.some((tab) => tab.key === nextKey);
      // The entry moves with the tab (interval kept, view back to default); with a merge the other tab keeps its own.
      moveTab(tabKey, nextKey, merged);
      if (setTabParams(tabKey, { symbol: next }) === null) setPicked(next);
    },
    [setTabParams, tabKey],
  );

  // On load, tabs that are not open lose their saved state.
  useEffect(() => {
    dropClosed(tabsRef.current.map((tab) => tab.key));
  }, []);

  // A new chart without an asset opens with the last chart seen, unless that asset already has its tab.
  useEffect(() => {
    if (params.symbol) return;
    const last = readLast();
    if (!last || tabsRef.current.some((tab) => tab.screen === "chart" && tab.params.symbol === last.symbol)) return;
    const nextKey = tabKeyFor("chart", { symbol: last.symbol });
    seedTab(nextKey, last.tf, last.view);
    if (setTabParams(tabKey, { symbol: last.symbol }) === null) dropTab(nextKey); // a bare screen has no tab to change
    // eslint-disable-next-line react-hooks/exhaustive-deps -- on mount only
  }, []);
  useEffect(() => {
    if (!params.symbol && picked) setTabTitle(tabKey, picked);
  }, [params.symbol, picked, setTabTitle, tabKey]);

  if (!symbol) {
    return (
      <Pane as="section" aria-label={t("screen.chart")} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", padding: 24, overflow: "auto" }}>
        <EmptyState icon="chart" title={t("chart.none.title")} description={t("chart.none.text")} />
        <div style={{ width: "min(420px, 100%)" }}>
          <AssetSearch onPick={changeSymbol} autoFocus />
        </div>
      </Pane>
    );
  }
  return <ChartView key={symbol} tabKey={tabKey} symbol={symbol} front={active} onSymbol={changeSymbol} />;
}

/** "Connect a live source": opens Connections with the group of the symbol's asset class in front. */
function ConnectLink({ assetClass }: { assetClass: string | null }) {
  const { t } = useI18n();
  const shell = useShell();
  return (
    <a
      className="live-link"
      href={connectionsHref(assetClass)}
      data-testid="connect-link"
      onClick={(event) => {
        event.preventDefault();
        requestFocus(assetClass);
        shell.openScreen("connections");
      }}
    >
      {t("chart.live.connect")}
    </a>
  );
}

type Popup = { kind: "search" | "interval"; initial: string } | null;

/** The text of a header warning pill; `zone` is the time zone of the exchange. */
function noticeText(notice: ChartNotice, zone: string, locale: string, t: ReturnType<typeof useI18n>["t"]): string {
  if (notice.kind === "approx_all") return t("chart.notice.approxAll");
  const time = formatMoment(notice.time, zone, locale);
  return notice.kind === "seconds_start" ? t("chart.notice.secondsStart", { time }) : t("chart.notice.approxBefore", { time });
}

interface ChartViewProps {
  tabKey: string;
  symbol: string;
  /** This tab is the one in front. */
  front: boolean;
  onSymbol: (symbol: string) => void;
}

function ChartView({ tabKey, symbol, front, onSymbol }: ChartViewProps) {
  const { t, locale } = useI18n();
  const shell = useShell();
  // What the tab remembered: read once, when the screen mounts.
  const [saved] = useState(() => readTab(tabKey));
  const [timeframe, setTimeframe] = useState<Timeframe>(() => saved?.tf ?? defaultInterval());
  /** The view to draw the chart with when it mounts: the saved one, then whatever the student leaves it at. */
  const viewRef = useRef<ChartViewState | null>(saved?.view ?? null);
  const frontRef = useRef(front);
  frontRef.current = front;
  const timeframeRef = useRef(timeframe);
  timeframeRef.current = timeframe;
  const [favorites, setFavorites] = useState<string[]>(loadFavorites);
  const [popup, setPopup] = useState<Popup>(null);
  const { live, data, retry } = useLiveChart(symbol, timeframe);
  const [asking, setAsking] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<PriceChartHandle>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLButtonElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  /** The focus goes back to the chart body (the element with the keyboard shortcuts) when a popover closes. */
  const focusRef = useMemo(
    () => ({ get current() { return bodyRef.current?.querySelector<HTMLElement>('[data-testid="chart-body"]') ?? null; } }) as RefObject<HTMLElement | null>,
    [],
  );

  const changeTimeframe = (next: Timeframe) => {
    if (next === timeframe) return;
    setTimeframe(next);
    saveInterval(next); // the old global fallback
    viewRef.current = null;
    cancelPending(tabKey);
    saveTabInterval(tabKey, symbol, next, frontRef.current);
  };
  const onViewChange = useCallback(
    (view: ChartViewState) => {
      viewRef.current = view;
      queueView(tabKey, symbol, timeframeRef.current, view, frontRef.current);
    },
    [tabKey, symbol],
  );
  // Being in front makes this the chart a new tab opens with.
  useEffect(() => {
    if (front) activateTab(tabKey, symbol, timeframeRef.current, viewRef.current);
  }, [front, tabKey, symbol]);
  // Closing the screen writes any movement still waiting.
  useEffect(() => flushPending, []);
  useChartShortcuts(bodyRef, {
    onSearch: (initial) => setPopup({ kind: "search", initial }),
    onInterval: (initial) => setPopup({ kind: "interval", initial }),
  });
  const closePopup = useCallback(() => setPopup(null), []);

  const ask = async (snapshot: ChartData) => {
    setAsking(true);
    try {
      const root = rootRef.current?.closest(".hk") ?? document.documentElement;
      const chart = chartRef.current;
      if (!chart) throw new Error("chart not ready");
      const image = await renderChartPng(snapshot, { root, t, locale, chart });
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

  const mode = barMode(live);
  const rounded = roundedRange(live);
  const delayed = live.dataState === "delayed";
  const minutes = delayMinutes(live);
  const change = data?.change_pct ?? null;
  const direction = change === null ? 0 : change > 0 ? 1 : change < 0 ? -1 : 0;
  const priceColor = direction < 0 ? "var(--down)" : "var(--up)";
  const lastMoment = data ? (data.last_trade_at ?? data.candles[data.candles.length - 1]?.t ?? data.fetched_at) : 0;
  const ready = data !== null && data.candles.length > 0;
  const error = live.error;
  const delayedLabel = minutes === null ? t("chart.live.delayed") : t("chart.live.delayedMinutes", { minutes });
  const historyMinutes = historyDelayMinutes(live);
  const label =
    mode === "live"
      ? live.source
        ? t("chart.route.live", { source: live.source })
        : t("chart.live.live")
      : mode === "reconnecting"
        ? t("chart.live.reconnecting")
        : mode === "delayed"
          ? delayedLabel
          : t("chart.market.closed");
  const lastData = mode === "closed" && data ? t("chart.lastData", { when: formatMoment(lastMoment, data.timezone, locale) }) : "";

  return (
    <Pane as="section" aria-label={`${symbol} ${timeframe}`} style={{ flex: 1, display: "flex", flexDirection: "column" }}>
      <div ref={rootRef} style={{ display: "contents" }}>
        <div style={{ position: "relative", display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", borderBottom: "1px solid var(--border)", flexWrap: "nowrap", minWidth: 0 }}>
          <span className="h2 mono" data-testid="chart-title" style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
            <button
              ref={titleRef}
              type="button"
              className="chart-symbol"
              data-testid="chart-symbol"
              aria-haspopup="dialog"
              aria-expanded={popup?.kind === "search"}
              aria-label={`${t("chart.search.open")}: ${symbol}`}
              title={t("chart.shortcuts.hint")}
              onClick={() => setPopup(popup?.kind === "search" ? null : { kind: "search", initial: "" })}
            >
              {symbol}
            </button>
            {" · "}
            {intervalLabel(timeframe, t)}
          </span>
          {data ? (
            <span className="p" style={{ fontSize: 12, minWidth: 0, flex: "0 1 auto", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {data.name}
              {data.exchange ? ` · ${data.exchange}` : ""}
            </span>
          ) : null}
          {data ? (
            <>
              <span className="mono" data-testid="chart-price" data-live={mode === "live" ? "true" : "false"} style={{ color: priceColor, whiteSpace: "nowrap", flexShrink: 0 }}>
                {formatPrice(data.last_price, locale)}
              </span>
              <Pill tone="acc" className={direction < 0 ? "bad" : undefined} style={NOWRAP} data-testid="chart-change">
                {formatPercent(data.change_pct, locale)}
              </Pill>
            </>
          ) : null}
          <div className="live-bar" role="status" data-testid="live-status" data-mode={mode} data-data-state={live.dataState}>
            <Pill tone={mode === "live" ? "ok" : mode === "closed" ? "default" : "warn"} style={NOWRAP} data-testid="market-state" title={lastData || undefined}>
              <Dot live={mode === "live"} />
              {label}
              {lastData ? ` · ${lastData}` : ""}
            </Pill>
            {delayed && mode === "closed" ? (
              <Pill tone="warn" style={NOWRAP} data-testid="delay-notice">
                {delayedLabel}
              </Pill>
            ) : null}
            {mode === "live" && historyMinutes !== null && live.history ? (
              <Pill tone="warn" style={NOWRAP} data-testid="history-notice">
                {t("chart.live.history", { source: live.history.source, minutes: historyMinutes })}
              </Pill>
            ) : null}
            {delayed && live.ready ? <ConnectLink assetClass={live.assetClass} /> : null}
            {live.notice ? (
              <Pill tone="warn" style={NOWRAP} data-testid="chart-notice" data-kind={live.notice.kind}>
                {noticeText(live.notice, live.timezone, locale, t)}
              </Pill>
            ) : null}
            {rounded !== null ? (
              <Pill tone="warn" style={NOWRAP} data-testid="range-rounded">
                {t("chart.notice.rounded", { size: formatPrice(rounded, locale) })}
              </Pill>
            ) : null}
          </div>
          <div style={{ marginLeft: "auto", flexShrink: 0, display: "flex", alignItems: "center", gap: 6 }}>
            <Segmented
              label={t("chart.timeframe")}
              value={timeframe}
              onChange={changeTimeframe}
              options={favorites.map((tf) => ({ value: tf, label: intervalLabel(tf, t) }))}
            />
            <button
              ref={moreRef}
              type="button"
              className="ib"
              data-testid="interval-more"
              aria-haspopup="dialog"
              aria-expanded={popup?.kind === "interval"}
              aria-label={t("chart.interval.more")}
              title={t("chart.interval.more")}
              onClick={() => setPopup(popup?.kind === "interval" ? null : { kind: "interval", initial: "" })}
            >
              +
            </button>
          </div>
          {popup?.kind === "search" ? (
            <AssetSearchPopover
              initialQuery={popup.initial}
              onPick={onSymbol}
              onClose={closePopup}
              returnFocusRef={focusRef}
              triggerRef={titleRef}
              style={{ top: "calc(100% + 4px)", left: 14 }}
            />
          ) : null}
          {popup?.kind === "interval" ? (
            <IntervalBox
              value={timeframe}
              favorites={favorites}
              initialText={popup.initial}
              onApply={changeTimeframe}
              onToggleFavorite={(interval) => setFavorites((list) => toggleFavorite(list, interval))}
              secondsDisabledReason={secondsDisabledReason(live, t)}
              ticksDisabledReason={ticksDisabledReason(live, t)}
              onClose={closePopup}
              returnFocusRef={focusRef}
              triggerRef={moreRef}
              style={{ top: "calc(100% + 4px)", right: 14 }}
            />
          ) : null}
        </div>

        <div ref={bodyRef} style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }}>

        {error ? (
          <div className="live-error" role="alert" data-testid="live-error" data-code={error.code}>
            <span className="h2">{t("chart.error.title")}</span>
            <p className="p">{errorText(error, t)}</p>
            <div className="live-bar">
              {CONNECTION_CODES.has(error.code) ? <ConnectLink assetClass={live.assetClass} /> : null}
              <Button size="sm" onClick={retry}>
                {t("ui.error.retry")}
              </Button>
            </div>
          </div>
        ) : !live.ready ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Loading label={t("chart.loading")} />
          </div>
        ) : data && !ready ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <EmptyState icon="chart" title={t("chart.empty.title")} description={t("chart.empty.text", { symbol, tf: timeframe })} />
          </div>
        ) : data ? (
          <PriceChart
            key={symbol + "|" + timeframe}
            ref={chartRef}
            initialView={viewRef.current}
            onViewChange={onViewChange}
            bars={data.candles}
            axis={data.interval?.kind === "range" ? "index" : "time"}
            timeframe={data.timeframe}
            timezone={data.timezone}
            opening={data.opening_range}
            label={t("chart.aria", { symbol: data.symbol, tf: data.timeframe })}
          />
        ) : null}
        </div>

        <div style={{ display: "flex", gap: 8, padding: "10px 14px", borderTop: "1px solid var(--border)", flexWrap: "wrap", alignItems: "center" }}>
          <span className="p" style={{ fontSize: 12 }}>
            {live.source ? t("chart.source", { source: live.source }) : ""}
          </span>
          <Button size="sm" variant="primary" style={{ marginLeft: "auto" }} disabled={!ready || asking || error !== null} onClick={() => data && void ask(data)}>
            {asking ? t("chart.ask.sending") : t("chart.ask")}
          </Button>
        </div>
      </div>
    </Pane>
  );
}
