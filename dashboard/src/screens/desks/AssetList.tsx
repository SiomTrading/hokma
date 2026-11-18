import { useEffect, useState } from "react";
import { api } from "@/api";
import { Pane } from "@/design";
import { useI18n } from "@/i18n";
import { formatPercent, formatPrice } from "../chart/format";
import type { ChartData } from "../chart/types";

/** How often the list asks for fresh quotes. */
export const QUOTE_REFRESH_MS = 60_000;

export interface Quote {
  last: number | null;
  previousClose: number | null;
  changePct: number | null;
}

/** A quote that could not be read shows as a dash, never as a made-up number. */
type QuoteState = Quote | "failed";

function quoteOf(data: ChartData): Quote {
  const lastBar = data.candles.length > 0 ? data.candles[data.candles.length - 1]!.c : null;
  return { last: data.last_price ?? lastBar, previousClose: data.previous_close, changePct: data.change_pct };
}

/** Last price and change of each symbol, from `GET /api/market/candles`, read again every 60 seconds. */
export function useQuotes(symbols: string[]): Record<string, QuoteState | undefined> {
  const [quotes, setQuotes] = useState<Record<string, QuoteState | undefined>>({});
  const key = symbols.join("|");

  useEffect(() => {
    let alive = true;
    const run = () => {
      for (const symbol of symbols) {
        api
          .get<ChartData>(`/api/market/candles?symbol=${encodeURIComponent(symbol)}&tf=5m`)
          .then((data) => alive && setQuotes((prev) => ({ ...prev, [symbol]: quoteOf(data) })))
          .catch(() => alive && setQuotes((prev) => ({ ...prev, [symbol]: prev[symbol] && prev[symbol] !== "failed" ? prev[symbol] : "failed" })));
      }
    };
    run();
    const timer = setInterval(run, QUOTE_REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // `key` stands for the list of symbols: a new array with the same symbols must not restart the polling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return quotes;
}

interface Props {
  symbols: string[];
  focus: string;
  onFocus: (symbol: string) => void;
}

/** The narrow list on the left of the terminal: the assets of the desk with last price and change. */
export function AssetList({ symbols, focus, onFocus }: Props) {
  const { t, locale } = useI18n();
  const quotes = useQuotes(symbols);
  const focused = quotes[focus];
  const focusQuote = focused && focused !== "failed" ? focused : null;

  return (
    <Pane className="dterm-list" aria-label={t("desks.terminal.list")}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{t("desks.terminal.watching")}</span>
      </div>
      <table className="dterm-table">
        <thead>
          <tr>
            <th scope="col">{t("desks.terminal.col.asset")}</th>
            <th scope="col" className="r">
              {t("desks.terminal.col.last")}
            </th>
            <th scope="col" className="r">
              {t("desks.terminal.col.change")}
            </th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((symbol) => {
            const state = quotes[symbol];
            const quote = state && state !== "failed" ? state : null;
            const tone = quote?.changePct === null || quote?.changePct === undefined ? "" : quote.changePct > 0 ? "up" : quote.changePct < 0 ? "down" : "";
            return (
              <tr key={symbol} className={symbol === focus ? "on" : undefined}>
                <td>
                  <button type="button" className="dterm-asset" aria-pressed={symbol === focus} onClick={() => onFocus(symbol)}>
                    {symbol}
                  </button>
                </td>
                <td className="r mono">{quote ? formatPrice(quote.last, locale) : "-"}</td>
                <td className={`r mono ${tone}`}>{quote ? formatPercent(quote.changePct, locale) : "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="dterm-quote">
        <span className="lbl">{t("desks.terminal.quote", { symbol: focus })}</span>
        <div className="mono dterm-quote-px">{focusQuote ? formatPrice(focusQuote.last, locale) : "-"}</div>
        <div className="mono hint">{t("desks.terminal.prevClose", { value: focusQuote ? formatPrice(focusQuote.previousClose, locale) : "-" })}</div>
      </div>
    </Pane>
  );
}
