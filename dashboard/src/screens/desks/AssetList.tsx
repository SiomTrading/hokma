import type { KeyboardEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { Button, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { formatPercent, formatPrice } from "../chart/format";
import { AssetSearch } from "../chart/AssetSearch";
import { LiveQuotesClient } from "../chart/liveQuotes";

export interface Quote {
  last: number | null;
  previousClose: number | null;
  changePct: number | null;
}

/** A quote that could not be read shows as a dash, never as a made-up number. */
type QuoteState = Quote | "failed";

/** Last price and change of each symbol, from the engine's stream (`/ws/market`): one socket, a subscription per symbol, painted as it arrives. No timer. */
export function useQuotes(symbols: string[]): Record<string, QuoteState | undefined> {
  const [quotes, setQuotes] = useState<Record<string, QuoteState | undefined>>({});
  const key = symbols.join("|");

  useEffect(() => {
    const client = new LiveQuotesClient({
      symbols,
      onQuote: (symbol, quote) =>
        setQuotes((prev) => {
          const before = prev[symbol];
          const held = before && before !== "failed" ? before : null;
          // A price message carries the price and the change; the previous close stays the one the snapshot gave.
          return { ...prev, [symbol]: { last: quote.last ?? held?.last ?? null, previousClose: quote.previousClose ?? held?.previousClose ?? null, changePct: quote.changePct ?? held?.changePct ?? null } };
        }),
      onFailed: (symbol) => setQuotes((prev) => ({ ...prev, [symbol]: prev[symbol] && prev[symbol] !== "failed" ? prev[symbol] : "failed" })),
    });
    client.start();
    return () => client.stop();
    // `key` stands for the list of symbols: a new array with the same symbols must not open the stream again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return quotes;
}

interface Props {
  symbols: string[];
  /** The asset in focus; null when the desk follows none. */
  focus: string | null;
  onFocus: (symbol: string) => void;
  /** Whether the search to add an asset is open. */
  adding: boolean;
  busy: boolean;
  error: string | null;
  onToggleAdd: () => void;
  onAdd: (symbol: string) => void;
  /** Takes the asset off the desk, after the student confirmed. */
  onRemove: (symbol: string) => void;
  /** The student moved an asset: the whole list in its new order. */
  onReorder: (symbols: string[]) => void;
}

/** The list with `symbol` taken out and put at `index` (an index of the list without it). */
export function moved(symbols: string[], symbol: string, index: number): string[] {
  const rest = symbols.filter((item) => item !== symbol);
  rest.splice(Math.max(0, Math.min(index, rest.length)), 0, symbol);
  return rest;
}

/** The narrow list on the left of the terminal: the assets of the desk with last price and change. */
export function AssetList({ symbols, focus, onFocus, adding, busy, error, onToggleAdd, onAdd, onRemove, onReorder }: Props) {
  const { t, locale } = useI18n();
  const [confirming, setConfirming] = useState<string | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const rows = useRef<HTMLDivElement>(null);
  const quotes = useQuotes(symbols);
  const focused = focus ? quotes[focus] : undefined;
  const focusQuote = focused && focused !== "failed" ? focused : null;

  /** Arrow up and down move the focus to the asset above or below, and the keyboard follows it. */
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, symbol: string) => {
    const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    if (event.altKey) {
      // Alt + arrow moves the asset itself one place; the focus stays on it.
      const at = symbols.indexOf(symbol);
      if (busy || at + step < 0 || at + step >= symbols.length) return;
      onReorder(moved(symbols, symbol, at + step));
      requestAnimationFrame(() => rows.current?.querySelectorAll<HTMLButtonElement>("button.dterm-asset")[at + step]?.focus());
      return;
    }
    const next = symbols[symbols.indexOf(symbol) + step];
    if (!next) return;
    onFocus(next);
    rows.current?.querySelectorAll<HTMLButtonElement>("button.dterm-asset")[symbols.indexOf(next)]?.focus();
  };

  const drop = (target: string) => {
    const from = dragging;
    setDragging(null);
    setOver(null);
    if (!from || from === target || busy) return;
    onReorder(moved(symbols, from, symbols.indexOf(target)));
  };

  return (
    <Pane className="dterm-list" aria-label={t("desks.terminal.list")}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{t("desks.terminal.watching")}</span>
        <span className="dterm-ph-end">
          <Button size="sm" variant="ghost" aria-expanded={adding} onClick={onToggleAdd}>
            {t("desks.terminal.add")}
          </Button>
        </span>
      </div>
      {adding ? (
        <div className="dterm-add">
          <AssetSearch compact autoFocus onPick={onAdd} />
          {busy ? <p className="hint">{t("desks.terminal.adding")}</p> : null}
        </div>
      ) : null}
      {error ? (
        <p className="err dterm-add-error" role="alert">
          {error}
        </p>
      ) : null}
      {symbols.length > 0 ? (
      <div className="dterm-rows" ref={rows}>
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
            <th scope="col" className="r">
              <span className="sr-only">{t("desks.terminal.col.remove")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((symbol) => {
            const state = quotes[symbol];
            const quote = state && state !== "failed" ? state : null;
            const tone = quote?.changePct === null || quote?.changePct === undefined ? "" : quote.changePct > 0 ? "up" : quote.changePct < 0 ? "down" : "";
            if (confirming === symbol) {
              return (
                <tr key={symbol} className="dterm-ask">
                  <td colSpan={4}>
                    <div className="dterm-confirm" role="group" aria-label={t("desks.terminal.remove.ask", { symbol })}>
                      <span className="dterm-confirm-q">{t("desks.terminal.remove.ask", { symbol })}</span>
                      <span className="dterm-confirm-btns">
                        <button
                          type="button"
                          className="dterm-yes"
                          disabled={busy}
                          onClick={() => {
                            setConfirming(null);
                            onRemove(symbol);
                          }}
                        >
                          {t("desks.terminal.remove.yes")}
                        </button>
                        <button type="button" className="dterm-no" onClick={() => setConfirming(null)}>
                          {t("desks.terminal.remove.no")}
                        </button>
                      </span>
                    </div>
                  </td>
                </tr>
              );
            }
            return (
              <tr
                key={symbol}
                className={[symbol === focus ? "on" : "", symbol === dragging ? "drag" : "", symbol === over && symbol !== dragging ? "over" : ""].filter(Boolean).join(" ") || undefined}
                draggable={!busy}
                data-symbol={symbol}
                onDragStart={(event) => {
                  setDragging(symbol);
                  event.dataTransfer?.setData("text/plain", symbol);
                  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
                }}
                onDragOver={(event) => {
                  if (!dragging) return;
                  event.preventDefault();
                  if (over !== symbol) setOver(symbol);
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  drop(symbol);
                }}
                onDragEnd={() => {
                  setDragging(null);
                  setOver(null);
                }}
              >
                <td>
                  <button type="button" className="dterm-asset" aria-pressed={symbol === focus} aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown" title={t("desks.terminal.reorder")} onClick={() => onFocus(symbol)} onKeyDown={(event) => onKeyDown(event, symbol)}>
                    {symbol}
                  </button>
                </td>
                <td className="r mono">{quote ? formatPrice(quote.last, locale) : "-"}</td>
                <td className={`r mono ${tone}`}>{quote ? formatPercent(quote.changePct, locale) : "-"}</td>
                <td className="r dterm-x">
                  <button type="button" className="ib dterm-remove" disabled={busy} aria-label={t("desks.field.asset.remove", { symbol })} onClick={() => setConfirming(symbol)}>
                    ✕
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
      ) : null}
      {focus ? (
      <div className="dterm-quote">
        <span className="lbl">{t("desks.terminal.quote", { symbol: focus })}</span>
        <div className="mono dterm-quote-px">{focusQuote ? formatPrice(focusQuote.last, locale) : "-"}</div>
        <div className="mono hint">{t("desks.terminal.prevClose", { value: focusQuote ? formatPrice(focusQuote.previousClose, locale) : "-" })}</div>
      </div>
      ) : null}
    </Pane>
  );
}
