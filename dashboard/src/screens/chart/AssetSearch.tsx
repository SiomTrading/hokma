import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, RefObject } from "react";
import { api } from "@/api";
import { Button, Dot, Input, MenuItem, Pill, cx } from "@/design";
import { useI18n } from "@/i18n";
import { ChartPopover, returnFocus } from "./ChartPopover";
import { loadRecent, pushRecent } from "./favorites";
import type { AssetHit, AssetRoute, AssetSearchResult, SearchClass } from "./types";

const DEBOUNCE_MS = 250;
const TICKER = /^[A-Z0-9^=.\-]{1,20}$/;

/** The class chips of the search, in order; `""` is All and sends no `class`. */
export const SEARCH_CLASSES: ReadonlyArray<SearchClass | ""> = ["", "future", "equity", "crypto", "forex", "index"];

export interface AssetSearchProps {
  onPick: (symbol: string) => void;
  autoFocus?: boolean;
  compact?: boolean;
  /**
   * `screen` (default) is the search of the empty chart, as it always was. `popover` is the one of the chart title:
   * class chips, recent symbols, the source of each result, and the keyboard (arrows move, Enter picks, Tab and
   * Shift+Tab change the chip).
   */
  variant?: "screen" | "popover";
  /** Text already in the field when it opens (the letter typed on the chart). */
  initialQuery?: string;
}

interface Row {
  symbol: string;
  name: string;
  exchange: string;
  route: AssetRoute | null;
  recent: boolean;
}

function RoutePill({ route }: { route: AssetRoute }) {
  const { t } = useI18n();
  const live = route.data_state === "live";
  return (
    <Pill tone={live ? "ok" : "warn"} data-testid="route-pill">
      {t(live ? "chart.route.live" : "chart.route.delayed", { source: route.source })}
    </Pill>
  );
}

/** Search box with its results. Empty text lists well-known tickers (and, in the popover, the recent ones first). */
export function AssetSearch({ onPick, autoFocus, compact, variant = "screen", initialQuery }: AssetSearchProps) {
  const { t } = useI18n();
  const popover = variant === "popover";
  const [query, setQuery] = useState(initialQuery ?? "");
  const [assetClass, setAssetClass] = useState<SearchClass | "">("");
  const [items, setItems] = useState<AssetHit[]>([]);
  const [liveClasses, setLiveClasses] = useState<string[]>([]);
  const [suggested, setSuggested] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [active, setActive] = useState(-1);
  const [recent] = useState<string[]>(() => (popover ? loadRecent() : []));
  const ids = useId();
  const inputId = `${ids}-input`;
  const listId = `${ids}-list`;
  const tries = useRef(0);

  useEffect(() => {
    const abort = new AbortController();
    const ticket = (tries.current += 1);
    const text = query.trim();
    setLoading(true);
    const path = `/api/market/symbols?q=${encodeURIComponent(text)}` + (assetClass ? `&class=${encodeURIComponent(assetClass)}` : "");
    const timer = setTimeout(
      () => {
        api
          .get<AssetSearchResult>(path, { signal: abort.signal })
          .then((result) => {
            if (ticket !== tries.current) return;
            setItems(Array.isArray(result.items) ? result.items : []);
            setSuggested(result.suggested);
            setLiveClasses(Array.isArray(result.live_classes) ? result.live_classes.filter((c): c is string => typeof c === "string") : []);
            setError(null);
          })
          .catch((failure: unknown) => {
            if (ticket !== tries.current || abort.signal.aborted) return;
            setItems([]);
            setError(failure instanceof Error && failure.message ? failure.message : t("chart.search.error"));
          })
          .finally(() => {
            if (ticket === tries.current) setLoading(false);
          });
      },
      text ? DEBOUNCE_MS : 0,
    );
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [query, assetClass, retry, t]);

  const pick = useCallback(
    (symbol: string) => {
      pushRecent(symbol);
      onPick(symbol);
    },
    [onPick],
  );
  const direct = query.trim().toUpperCase();

  /** Popover rows: recent symbols first while the field is empty and All is chosen, then the results. */
  const rows = useMemo<Row[]>(() => {
    const fromHit = (hit: AssetHit, isRecent = false): Row => ({ symbol: hit.symbol, name: hit.name, exchange: hit.exchange, route: hit.route ?? null, recent: isRecent });
    const results = items.map((hit) => fromHit(hit));
    if (!popover || query.trim() || assetClass) return results;
    const upper = (s: string) => s.toUpperCase();
    const recentRows = recent.map((symbol) => {
      const hit = items.find((item) => upper(item.symbol) === upper(symbol));
      return hit ? fromHit(hit, true) : { symbol, name: "", exchange: "", route: null, recent: true };
    });
    const taken = new Set(recent.map(upper));
    return [...recentRows, ...results.filter((row) => !taken.has(upper(row.symbol)))];
  }, [items, popover, query, assetClass, recent]);

  useEffect(() => setActive(-1), [rows]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (popover && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      if (rows.length === 0) return;
      event.preventDefault();
      setActive((at) => (event.key === "ArrowDown" ? (at + 1) % rows.length : (at - 1 + rows.length) % rows.length));
      return;
    }
    if (popover && event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      const at = SEARCH_CLASSES.indexOf(assetClass);
      const step = event.shiftKey ? -1 : 1;
      setAssetClass(SEARCH_CLASSES[(at + step + SEARCH_CLASSES.length) % SEARCH_CLASSES.length]!);
      return;
    }
    if (event.key !== "Enter") return;
    const chosen = popover && active >= 0 ? rows[active] : undefined;
    if (chosen) {
      event.preventDefault();
      pick(chosen.symbol);
      return;
    }
    if (direct && TICKER.test(direct)) {
      event.preventDefault();
      const fresh = !suggested && !loading; // results that belong to what was typed
      const match = fresh ? (items.find((item) => item.symbol.toUpperCase() === direct) ?? items[0]) : undefined;
      pick(match?.symbol ?? direct);
    }
  };

  const recentRows = rows.filter((row) => row.recent);
  const resultRows = rows.filter((row) => !row.recent);
  const optionId = (index: number) => `${ids}-opt-${index}`;

  const renderOption = (row: Row, index: number) => (
    <div
      key={`${row.recent ? "r" : "s"}-${row.symbol}`}
      id={optionId(index)}
      role="option"
      aria-selected={index === active}
      className={cx("chart-opt", index === active && "active")}
      title={row.name ? `${row.name}${row.exchange ? ` (${row.exchange})` : ""}` : row.symbol}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => pick(row.symbol)}
    >
      <span className="mono">{row.symbol}</span>
      {row.name ? <span className="name">{row.name}</span> : null}
      {row.route ? <RoutePill route={row.route} /> : null}
    </div>
  );

  return (
    <div style={popover ? { display: "flex", flexDirection: "column", gap: 6 } : undefined}>
      <label htmlFor={inputId} style={{ position: "absolute", left: -9999 }}>
        {t("chart.search.label")}
      </label>
      <Input
        id={inputId}
        autoFocus={autoFocus}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onFocus={(event) => {
          const end = event.target.value.length;
          event.target.setSelectionRange(end, end);
        }}
        onKeyDown={onKeyDown}
        placeholder={t("chart.search.placeholder")}
        autoComplete="off"
        spellCheck={false}
        maxLength={40}
        {...(popover
          ? {
              role: "combobox",
              "aria-expanded": rows.length > 0,
              "aria-controls": listId,
              "aria-autocomplete": "list" as const,
              "aria-activedescendant": active >= 0 && rows[active] ? optionId(active) : undefined,
            }
          : null)}
        style={compact || popover ? { minHeight: 34, margin: "2px 0 4px", fontSize: 12 } : undefined}
      />
      {popover ? (
        <div className="chart-chips" role="tablist" aria-label={t("chart.search.classes")}>
          {SEARCH_CLASSES.map((cls) => {
            const live = cls === "" ? liveClasses.length > 0 : liveClasses.includes(cls);
            return (
              <button
                key={cls || "all"}
                type="button"
                role="tab"
                tabIndex={-1}
                aria-selected={assetClass === cls}
                aria-controls={listId}
                className="chart-chip"
                title={live ? t("chart.search.liveClass") : undefined}
                data-live={live ? "true" : undefined}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => setAssetClass(cls)}
              >
                {live ? <Dot color="var(--ok)" /> : null}
                {t(`chart.class.${cls || "all"}`)}
              </button>
            );
          })}
        </div>
      ) : null}
      <div aria-live="polite">
        {loading && items.length === 0 && !error ? (
          <p className="p" style={{ padding: "8px 10px", fontSize: 12 }}>
            {t("chart.search.loading")}
          </p>
        ) : null}
        {error ? (
          <div style={{ padding: "8px 10px", display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-start" }}>
            <p className="p" role="alert" style={{ fontSize: 12 }}>
              {error}
            </p>
            <Button size="sm" onClick={() => setRetry((n) => n + 1)}>
              {t("ui.error.retry")}
            </Button>
          </div>
        ) : null}
        {!loading && !error && rows.length === 0 ? (
          <p className="p" style={{ padding: "8px 10px", fontSize: 12 }}>
            {t("chart.search.empty", { query: query.trim() })}
          </p>
        ) : null}
        {!popover && !error && items.length > 0 ? (
          <div role="group" className="chart-menu-list" aria-label={suggested ? t("chart.search.suggested") : t("chart.search.results")}>
            {items.map((item) => (
              <MenuItem key={item.symbol} dotColor="var(--org)" hint={`${t(`chart.kind.${item.kind}`)}`} onClick={() => pick(item.symbol)} title={`${item.name} (${item.exchange})`}>
                <span className="mono">{item.symbol}</span>
                <span className="p" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: "0 1 auto" }}>
                  {item.name}
                </span>
                {item.route ? <RoutePill route={item.route} /> : null}
              </MenuItem>
            ))}
          </div>
        ) : null}
        {popover && !error && rows.length > 0 ? (
          <div className="chart-pop-list" role="listbox" id={listId} aria-label={t("chart.search.results")}>
            {recentRows.length > 0 ? (
              <div role="group" aria-label={t("chart.search.recent")}>
                <span className="chart-pop-head" aria-hidden="true">
                  {t("chart.search.recent")}
                </span>
                {recentRows.map((row, index) => renderOption(row, index))}
              </div>
            ) : null}
            {resultRows.length > 0 ? (
              <div role="group" aria-label={suggested ? t("chart.search.suggested") : t("chart.search.results")}>
                {recentRows.length > 0 ? (
                  <span className="chart-pop-head" aria-hidden="true">
                    {suggested ? t("chart.search.suggested") : t("chart.search.results")}
                  </span>
                ) : null}
                {resultRows.map((row, index) => renderOption(row, recentRows.length + index))}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export interface AssetSearchPopoverProps {
  /** Receives the symbol picked. The popover closes right after and gives the focus back. */
  onPick: (symbol: string) => void;
  onClose: () => void;
  initialQuery?: string;
  returnFocusRef?: RefObject<HTMLElement | null>;
  triggerRef?: RefObject<HTMLElement | null>;
  style?: CSSProperties;
  className?: string;
}

/** The symbol search of the chart title, in its popover. Esc closes it and the focus goes back to the chart. */
export function AssetSearchPopover({ onPick, onClose, initialQuery, returnFocusRef, triggerRef, style, className }: AssetSearchPopoverProps) {
  const { t } = useI18n();
  const choose = useCallback(
    (symbol: string) => {
      onPick(symbol);
      onClose();
      returnFocus(returnFocusRef);
    },
    [onPick, onClose, returnFocusRef],
  );
  return (
    <ChartPopover label={t("chart.search.label")} onClose={onClose} returnFocusRef={returnFocusRef} triggerRef={triggerRef} style={style} className={cx("wide", className)}>
      <AssetSearch variant="popover" autoFocus initialQuery={initialQuery} onPick={choose} />
    </ChartPopover>
  );
}
