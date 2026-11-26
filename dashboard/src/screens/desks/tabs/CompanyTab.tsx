import { useCallback, useEffect, useState } from "react";
import { ErrorState, Loading, Select } from "@/design";
import { useI18n } from "@/i18n";
import { formatPercent, formatPrice, formatVolume } from "../../chart/format";
import { useQuotes } from "../AssetList";
import { companyApi } from "../companyApi";
import type { Block, CompanyData } from "../companyApi";
import { deskConfigApi, failText } from "../configApi";
import { DeskEmptyState } from "../EmptyState";
import { loadFocusAsset, saveFocusAsset } from "../terminalPrefs";
import type { TabProps } from "./types";

type Translate = ReturnType<typeof useI18n>["t"];
type Assets = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; list: string[] };
type Data = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; data: CompanyData };

/** Company of a desk (SPEC-025, delivery 3): the data of one asset of the desk's list, in dense tables. The price follows the stream. */
export function CompanyTab({ desk, focusSymbol, onFocusUsed }: TabProps) {
  const { t } = useI18n();
  const [assets, setAssets] = useState<Assets>({ state: "loading" });
  const [symbol, setSymbol] = useState<string | null>(null);

  const readAssets = useCallback(() => {
    setAssets({ state: "loading" });
    deskConfigApi
      .detail(desk.id)
      .then((detail) => {
        setAssets({ state: "ready", list: detail.assets });
        const saved = loadFocusAsset(desk.id);
        setSymbol(focusSymbol ?? (saved && detail.assets.includes(saved) ? saved : (detail.assets[0] ?? null)));
      })
      .catch((cause) => setAssets({ state: "error", message: failText(cause, t("desks.company.error")) }));
    // `focusSymbol` only decides which asset is shown when the list is read; a new one is handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desk.id, t]);

  useEffect(readAssets, [readAssets]);
  useEffect(() => {
    if (!focusSymbol) return;
    setSymbol(focusSymbol);
    onFocusUsed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusSymbol]);

  if (assets.state === "loading") return <Loading label={t("desks.company.loading")} />;
  if (assets.state === "error") return <ErrorState message={assets.message} onRetry={readAssets} />;
  if (symbol === null) {
    return <DeskEmptyState icon="chart" title={t("desks.company.empty.title")} text={t("desks.company.empty.text")} />;
  }
  return (
    <section className="company-tab" aria-label={t("desks.tab.company")}>
      <div className="company-pick">
        <span className="lbl">{t("desks.company.asset")}</span>
        <Select
          aria-label={t("desks.company.asset")}
          value={symbol}
          onChange={(event) => {
            setSymbol(event.target.value);
            saveFocusAsset(desk.id, event.target.value);
          }}
        >
          {(symbol && !assets.list.includes(symbol) ? [...assets.list, symbol] : assets.list).map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </Select>
      </div>
      <CompanyBody key={symbol} deskId={desk.id} symbol={symbol} />
    </section>
  );
}

function CompanyBody({ deskId, symbol }: { deskId: number; symbol: string }) {
  const { t, locale } = useI18n();
  const [load, setLoad] = useState<Data>({ state: "loading" });
  const quotes = useQuotes([symbol]);

  const read = useCallback(() => {
    setLoad({ state: "loading" });
    companyApi
      .get(deskId, symbol)
      .then((data) => setLoad({ state: "ready", data }))
      .catch((cause) => setLoad({ state: "error", message: failText(cause, t("desks.company.error")) }));
  }, [deskId, symbol, t]);

  useEffect(read, [read]);

  const state = quotes[symbol];
  const quote = state && state !== "failed" ? state : null;
  const tone = quote?.changePct == null ? "" : quote.changePct > 0 ? "up" : quote.changePct < 0 ? "down" : "";

  return (
    <div className="company-grid">
      <Panel title={t("desks.company.quote")}>
        <table className="dterm-table company-table">
          <tbody>
            <Row label={t("desks.company.last")} value={quote ? formatPrice(quote.last, locale) : "-"} />
            <Row label={t("desks.company.change")} value={quote ? formatPercent(quote.changePct, locale) : "-"} tone={tone} />
            <Row label={t("desks.company.volume")} value={quote?.volume != null ? formatVolume(quote.volume, locale) : "-"} />
            <Row label={t("desks.company.feed")} value={quote?.source ? `${quote.source}${quote.delayed ? " · " + t("desks.company.delayed") : ""}` : "-"} />
          </tbody>
        </table>
        {state === "failed" ? <p className="company-none">{t("desks.company.quoteFailed")}</p> : null}
      </Panel>
      {load.state === "loading" ? <Loading label={t("desks.company.loading")} /> : null}
      {load.state === "error" ? <ErrorState message={load.message} onRetry={read} /> : null}
      {load.state === "ready" ? <Blocks data={load.data} /> : null}
    </div>
  );
}

function Blocks({ data }: { data: CompanyData }) {
  const { t, locale } = useI18n();
  const money = data.profile.ok ? (data.profile.currency ?? "USD") : "USD";
  const next = data.events.earnings.ok ? data.events.earnings.next : null;
  return (
    <>
      <Panel title={t("desks.company.fundamentals")}>
        <Filled block={data.fundamentals} t={t}>
          {(b) => (
            <table className="dterm-table company-table">
              <tbody>
                <Row label={t("desks.company.f.cap")} value={cap(b.ratios.market_cap_millions, money, locale)} />
                <Row label={t("desks.company.f.beta")} value={plain(b.ratios.beta, locale)} />
                <Row label={t("desks.company.f.pe")} value={plain(b.ratios.pe, locale)} />
                <Row label={t("desks.company.f.eps")} value={plain(b.ratios.eps, locale)} />
                <Row label={t("desks.company.f.margin")} value={pct(b.ratios.operating_margin, locale)} />
                <Row label={t("desks.company.f.debt")} value={plain(b.ratios.debt_to_equity, locale)} />
                <Row label={t("desks.company.f.growth")} value={pct(b.ratios.revenue_growth, locale)} />
              </tbody>
            </table>
          )}
        </Filled>
      </Panel>
      <Panel title={t("desks.company.events")}>
        <Filled block={data.events.earnings} t={t}>
          {() => (
            <table className="dterm-table company-table">
              <tbody>
                <Row
                  label={t("desks.company.nextReport")}
                  value={next ? [day(next.date, locale), next.hour ? t(`desks.company.hour.${next.hour}`) : "", next.eps_estimate != null ? t("desks.company.epsEst", { n: plain(next.eps_estimate, locale) }) : ""].filter(Boolean).join(" · ") : t("desks.company.noReport")}
                />
              </tbody>
            </table>
          )}
        </Filled>
        <Filled block={data.events.expirations} t={t}>
          {(b) => (
            <table className="dterm-table company-table">
              <thead>
                <tr>
                  <th scope="col">{t("desks.company.expiration")}</th>
                  <th scope="col" className="r">
                    {t("desks.company.days")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {b.items.length === 0 ? (
                  <tr>
                    <td colSpan={2}>{t("desks.company.noExpirations")}</td>
                  </tr>
                ) : (
                  b.items.map((row) => (
                    <tr key={row.date}>
                      <td className="mono">{day(row.date, locale)}</td>
                      <td className="r mono">{row.days ?? "-"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          )}
        </Filled>
      </Panel>
      <Panel title={t("desks.company.results")}>
        <Filled block={data.results} t={t}>
          {(b) => (
            <table className="dterm-table company-table">
              <thead>
                <tr>
                  <th scope="col">{t("desks.company.quarter")}</th>
                  <th scope="col" className="r">
                    {t("desks.company.epsEstimate")}
                  </th>
                  <th scope="col" className="r">
                    {t("desks.company.epsActual")}
                  </th>
                  <th scope="col" className="r">
                    {t("desks.company.surprise")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row) => (
                  <tr key={row.period}>
                    <td className="mono">{row.year && row.quarter ? `${row.year} T${row.quarter}` : day(row.period, locale)}</td>
                    <td className="r mono">{plain(row.eps_estimate, locale)}</td>
                    <td className="r mono">{plain(row.eps_actual, locale)}</td>
                    <td className={`r mono ${row.surprise_pct == null ? "" : row.surprise_pct >= 0 ? "up" : "down"}`}>{formatPercent(row.surprise_pct, locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Filled>
      </Panel>
      <Panel title={t("desks.company.news")} wide>
        <Filled block={data.news} t={t}>
          {(b) => (
            <table className="dterm-table company-table">
              <thead>
                <tr>
                  <th scope="col">{t("desks.company.date")}</th>
                  <th scope="col">{t("desks.company.source")}</th>
                  <th scope="col">{t("desks.company.headline")}</th>
                </tr>
              </thead>
              <tbody>
                {b.items.map((item, index) => (
                  <tr key={`${item.time}-${index}`}>
                    <td className="mono">{item.time ? day(item.time, locale) : "-"}</td>
                    <td>{item.source}</td>
                    <td>
                      {item.url && /^https?:\/\//.test(item.url) ? (
                        <a href={item.url} target="_blank" rel="noopener noreferrer">
                          {item.headline}
                        </a>
                      ) : (
                        item.headline
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Filled>
      </Panel>
    </>
  );
}

function Panel({ title, wide, children }: { title: string; wide?: boolean; children: React.ReactNode }) {
  return (
    <section className={wide ? "company-block wide" : "company-block"}>
      <h3 className="lbl">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td className={`r mono ${tone ?? ""}`}>{value}</td>
    </tr>
  );
}

/** A block with data shows it; a block without says "No data from {provider}" and never a 0. */
function Filled<T>({ block, t, children }: { block: Block<T>; t: Translate; children: (filled: Extract<Block<T>, { ok: true }>) => React.ReactNode }) {
  if (!block.ok) {
    return (
      <p className="company-none" title={block.reason}>
        {t("desks.company.noData", { source: block.source })}
      </p>
    );
  }
  return <>{children(block as Extract<Block<T>, { ok: true }>)}</>;
}

const plain = (value: number | null | undefined, locale: string): string =>
  value == null || !Number.isFinite(value) ? "-" : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value);
const pct = (value: number | null | undefined, locale: string): string => (value == null || !Number.isFinite(value) ? "-" : `${plain(value, locale)}%`);
const cap = (millions: number | null | undefined, currency: string, locale: string): string =>
  millions == null || !Number.isFinite(millions) ? "-" : new Intl.NumberFormat(locale, { style: "currency", currency, notation: "compact", maximumFractionDigits: 1 }).format(millions * 1_000_000);
/** A date as the person reads it. A date without hour is shown as that same day, whatever the time zone of the computer. */
const day = (value: string, locale: string): string => {
  const moment = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00Z` : value);
  return Number.isNaN(moment.getTime()) ? value : new Intl.DateTimeFormat(locale, { timeZone: "UTC", day: "numeric", month: "short", year: "numeric" }).format(moment);
};
