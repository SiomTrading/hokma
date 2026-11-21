import { useState } from "react";
import { useI18n } from "@/i18n";
import { decimal, priced, whole } from "./format";
import type { UnderlyingBar as Bar } from "./types";

/** The row above the chain: last, net change, bid, ask, size, volume, open, high and low. Folds to its title. */
export function UnderlyingBar({ bar }: { bar: Bar | null }) {
  const { t, locale } = useI18n();
  const [open, setOpen] = useState(true);
  const change = bar?.net_change ?? null;
  const direction = change === null ? "" : change < 0 ? "opt-down" : change > 0 ? "opt-up" : "";
  const size =
    bar && (bar.bid_size !== null || bar.ask_size !== null)
      ? t("options.u.sizeValue", { bid: whole(bar.bid_size, locale, t), ask: whole(bar.ask_size, locale, t) })
      : t("options.missing");
  const fields: Array<{ key: string; label: string; value: string; className?: string }> = [
    { key: "last", label: t("options.u.last"), value: priced(bar?.last, bar?.last_exchange, locale, t) },
    {
      key: "net",
      label: t("options.u.netChange"),
      value: change === null ? t("options.missing") : (change > 0 ? "+" : "") + decimal(change, 2, locale, t),
      className: direction,
    },
    { key: "bid", label: t("options.u.bid"), value: priced(bar?.bid, bar?.bid_exchange, locale, t) },
    { key: "ask", label: t("options.u.ask"), value: priced(bar?.ask, bar?.ask_exchange, locale, t) },
    { key: "size", label: t("options.u.size"), value: size },
    { key: "volume", label: t("options.u.volume"), value: whole(bar?.volume, locale, t) },
    { key: "open", label: t("options.u.open"), value: decimal(bar?.open, 2, locale, t) },
    { key: "high", label: t("options.u.high"), value: decimal(bar?.high, 2, locale, t) },
    { key: "low", label: t("options.u.low"), value: decimal(bar?.low, 2, locale, t) },
  ];
  return (
    <section className="opt-under" aria-label={t("options.underlying.title")} data-testid="options-underlying">
      <button type="button" className="opt-fold" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span> {t("options.underlying.title")}
      </button>
      {open ? (
        <dl className="opt-fields">
          {fields.map((field) => (
            <div key={field.key} className="opt-field" data-field={field.key}>
              <dt>{field.label}</dt>
              <dd className={"mono " + (field.className ?? "")}>{field.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
  );
}
