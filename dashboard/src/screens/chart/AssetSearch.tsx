import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "@/api";
import { Button, Input, MenuItem } from "@/design";
import { useI18n } from "@/i18n";
import type { AssetHit, AssetSearchResult } from "./types";

const DEBOUNCE_MS = 250;

interface Props {
  onPick: (symbol: string) => void;
  autoFocus?: boolean;
  compact?: boolean;
}

/** Search box with its results. Empty text lists well-known tickers; typing searches the data source. */
export function AssetSearch({ onPick, autoFocus, compact }: Props) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<AssetHit[]>([]);
  const [suggested, setSuggested] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const inputId = useId();
  const tries = useRef(0);

  useEffect(() => {
    const abort = new AbortController();
    const ticket = (tries.current += 1);
    const text = query.trim();
    setLoading(true);
    const timer = setTimeout(
      () => {
        api
          .get<AssetSearchResult>(`/api/market/symbols?q=${encodeURIComponent(text)}`, { signal: abort.signal })
          .then((result) => {
            if (ticket !== tries.current) return;
            setItems(result.items);
            setSuggested(result.suggested);
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
  }, [query, retry, t]);

  const pick = useCallback((symbol: string) => onPick(symbol), [onPick]);
  const direct = query.trim().toUpperCase();

  return (
    <div>
      <label htmlFor={inputId} style={{ position: "absolute", left: -9999 }}>
        {t("chart.search.label")}
      </label>
      <Input
        id={inputId}
        autoFocus={autoFocus}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && direct && /^[A-Z0-9^=.\-]{1,20}$/.test(direct)) {
            event.preventDefault();
            const fresh = !suggested && !loading; // results that belong to what was typed
            const match = fresh ? (items.find((item) => item.symbol.toUpperCase() === direct) ?? items[0]) : undefined;
            pick(match?.symbol ?? direct);
          }
        }}
        placeholder={t("chart.search.placeholder")}
        autoComplete="off"
        spellCheck={false}
        maxLength={40}
        style={compact ? { minHeight: 34, margin: "2px 0 4px", fontSize: 12 } : undefined}
      />
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
        {!loading && !error && items.length === 0 ? (
          <p className="p" style={{ padding: "8px 10px", fontSize: 12 }}>
            {t("chart.search.empty", { query: query.trim() })}
          </p>
        ) : null}
        {!error && items.length > 0 ? (
          <div role="group" aria-label={suggested ? t("chart.search.suggested") : t("chart.search.results")}>
            {items.map((item) => (
              <MenuItem key={item.symbol} dotColor="var(--org)" hint={`${t(`chart.kind.${item.kind}`)}`} onClick={() => pick(item.symbol)} title={`${item.name} (${item.exchange})`}>
                <span className="mono">{item.symbol}</span>
                <span className="p" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: "0 1 auto" }}>
                  {item.name}
                </span>
              </MenuItem>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
