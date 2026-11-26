import { useI18n } from "@/i18n";
import type { PnlView } from "./pnlApi";
import { resultTone } from "./format";

/** The hour of a reading, in the clock of the owner. */
export function readTime(iso: string | null | undefined, locale: string): string {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString(locale, { hour12: false }) : "-";
}

/** The name of the broker that gave the reading, from the catalog of Connections (the only place that names providers). A broker with no entry there is shown as "the broker". */
export function providerName(t: (key: string) => string, source: string | null | undefined): string | null {
  if (!source) return null;
  const key = `connections.provider.${source}`;
  const text = t(key);
  return text === key ? null : text;
}

function signed(value: number, locale: string, currency: string): string {
  const text = Math.abs(value).toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}${text} ${currency}`;
}

/** The name of the broker that gave the number, from its id (its id with a capital: "schwab" shows Schwab): the owner sees whose number it is, to know where to fix it. */
export function brokerName(id: string | null | undefined): string {
  return id ? id.charAt(0).toUpperCase() + id.slice(1) : "";
}

/** The day P&L of an account as its BROKER gives it, with the hour of the last reading. Without data it says so; it never shows an invented number. */
export function PnlCell({ view, currency }: { view: PnlView | undefined; currency: string }) {
  const { t, locale } = useI18n();
  if (!view || view.state === "pending") {
    return <span className="brk-cost-note" data-testid="pnl-pending">{t("desks.accounts.pnl.pending")}</span>;
  }
  if (view.state === "unavailable") {
    const unnamed = providerName(t as (key: string) => string, view.source);
    return (
      <span className="brk-cost-note" data-testid="pnl-unavailable">
        {t("desks.accounts.pnl.unavailable")}
        {unnamed ? " (" + unnamed + ")" : ""}
      </span>
    );
  }
  const last = view.last;
  const name = providerName(t as (key: string) => string, view.source ?? last?.source);
  const source = t(view.source === "sim" ? "desks.accounts.pnl.source.sim" : "desks.accounts.pnl.source.broker", { broker: brokerName(view.source ?? last?.source) });
  if (view.state === "no_data") {
    return (
      <span data-testid="pnl-no-data" role="status">
        <b className="err">{t("desks.accounts.pnl.noData", { time: readTime(view.since, locale) })}</b>
        {name ? <small className="brk-cost-note" data-testid="pnl-provider"> {name}</small> : null}
        {last && last.day_pnl !== null ? (
          <small className="brk-cost-note"> {t("desks.accounts.pnl.lastKnown", { value: signed(last.day_pnl, locale, currency), time: readTime(last.as_of, locale) })}</small>
        ) : null}
      </span>
    );
  }
  const day = last?.day_pnl ?? null;
  return (
    <span data-testid="pnl-ok">
      <b className={"mono " + (day === null ? "" : resultTone(day))}>{day === null ? t("desks.accounts.unavailable") : signed(day, locale, currency)}</b>
      <small className="brk-cost-note"> {t("desks.accounts.pnl.read", { time: readTime(last?.as_of, locale), source })}</small>
    </span>
  );
}
