import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input, Loading, Pill, Select } from "@/design";
import { useI18n } from "@/i18n";
import { formatPrice } from "../../chart/format";
import { useQuotes } from "../AssetList";
import { deskConfigApi, failText } from "../configApi";
import { loadFocusAsset } from "../terminalPrefs";
import { detailApi, STRIKES_OF, requestOfCard } from "../optionDetailApi";
import { ProposeControls } from "../ProposeControls";
import { PRIORITIES, strategiesApi, VIEWS } from "../strategiesApi";
import type { Card, Priority, StrategiesResult, View } from "../strategiesApi";
import type { TabProps } from "./types";

type Translate = ReturnType<typeof useI18n>["t"];
const AUTO = "";
const SYMBOL = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/;

/**
 * Strategies of a desk (SPEC-028, delivery 5): the owner says what they expect of a stock and the panel shows the strategies that serve it on the real
 * chain, each with its payoff chart, its figures and the verdict of the options analyst of the desk. It asks the server only when the owner presses the
 * button (or the chief asks by chat); the price follows the stream. Nothing here sends an order.
 */
export function StrategiesTab({ desk, openPanel }: TabProps) {
  const { t, locale } = useI18n();
  const [assets, setAssets] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [committed, setCommitted] = useState("");
  const [view, setView] = useState<View>("bullish");
  const [target, setTarget] = useState("");
  const [budget, setBudget] = useState("");
  const [expiration, setExpiration] = useState(AUTO);
  const [priority, setPriority] = useState<Priority>("balanced");
  const [expirations, setExpirations] = useState<Array<{ date: string; days: number }>>([]);
  const [result, setResult] = useState<StrategiesResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const asked = useRef(0);

  // The panel opens with the last consultation of the desk (the one the chief made by chat, or the one left by the owner) or with the asset in focus.
  useEffect(() => {
    let alive = true;
    const detail = deskConfigApi.detail(desk.id).then((d) => d.assets).catch(() => [] as string[]);
    const last = strategiesApi.last(desk.id).catch(() => null);
    void Promise.all([detail, last]).then(([list, saved]) => {
      if (!alive) return;
      setAssets(list);
      const form = saved?.form;
      if (form) {
        setSymbol(form.symbol);
        setCommitted(form.symbol);
        setView(form.view);
        setTarget(form.target == null ? "" : String(form.target));
        setBudget(form.budget == null ? "" : String(form.budget));
        setExpiration(form.expiration ?? AUTO);
        setPriority(form.priority);
        setResult(saved?.result ?? null);
      } else {
        const focus = loadFocusAsset(desk.id);
        const first = focus && list.includes(focus) ? focus : (list[0] ?? "");
        setSymbol(first);
        setCommitted(first);
      }
      setReady(true);
    });
    return () => {
      alive = false;
    };
  }, [desk.id]);

  // The expirations of the chain are read when the asset changes, not on a timer.
  useEffect(() => {
    if (!SYMBOL.test(committed)) {
      setExpirations([]);
      return;
    }
    let alive = true;
    strategiesApi
      .expirations(committed)
      .then((r) => alive && setExpirations(r.expirations ?? []))
      .catch(() => alive && setExpirations([]));
    return () => {
      alive = false;
    };
  }, [committed]);

  const quotes = useQuotes(SYMBOL.test(committed) ? [committed.toUpperCase()] : []);
  const state = quotes[committed.toUpperCase()];
  const quote = state && state !== "failed" ? state : null;

  const submit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      const clean = symbol.trim().toUpperCase();
      if (!SYMBOL.test(clean)) {
        setFieldError(t("desks.strategies.symbolInvalid"));
        return;
      }
      setFieldError(null);
      setCommitted(clean);
      const number = (text: string) => (text.trim() === "" ? null : Number(text.replace(",", ".")));
      const body = { symbol: clean, view, target: number(target), budget: number(budget), expiration: expiration === AUTO ? null : expiration, priority };
      const turn = ++asked.current;
      setBusy(true);
      setError(null);
      strategiesApi
        .ask(desk.id, body)
        .then((data) => turn === asked.current && setResult(data))
        .catch((cause) => turn === asked.current && setError(failText(cause, t("desks.strategies.error"))))
        .finally(() => turn === asked.current && setBusy(false));
    },
    [symbol, view, target, budget, expiration, priority, desk.id, t],
  );

  if (!ready) return <Loading label={t("desks.strategies.loading")} />;
  return (
    <section className="strat-tab" aria-label={t("desks.tab.strategies")}>
      <form className="strat-form" onSubmit={submit} noValidate>
        <Field label={t("desks.strategies.asset")} error={fieldError ?? undefined}>
          {(props) => (
            <>
              <Input
                {...props}
                list={`strat-assets-${desk.id}`}
                value={symbol}
                maxLength={10}
                autoComplete="off"
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                onBlur={() => SYMBOL.test(symbol.trim()) && setCommitted(symbol.trim().toUpperCase())}
              />
              <datalist id={`strat-assets-${desk.id}`}>
                {assets.map((a) => (
                  <option key={a} value={a} />
                ))}
              </datalist>
            </>
          )}
        </Field>
        <Field label={t("desks.strategies.view")}>
          {(props) => (
            <Select {...props} value={view} onChange={(e) => setView(e.target.value as View)}>
              {VIEWS.map((v) => (
                <option key={v} value={v}>
                  {t(`desks.strategies.view.${v}`)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("desks.strategies.target")}>
          {(props) => <Input {...props} inputMode="decimal" value={target} placeholder={t("desks.strategies.optional")} onChange={(e) => setTarget(e.target.value)} />}
        </Field>
        <Field label={t("desks.strategies.budget")}>
          {(props) => <Input {...props} inputMode="decimal" value={budget} placeholder={t("desks.strategies.optional")} onChange={(e) => setBudget(e.target.value)} />}
        </Field>
        <Field label={t("desks.strategies.expiration")}>
          {(props) => (
            <Select {...props} value={expiration} onChange={(e) => setExpiration(e.target.value)}>
              <option value={AUTO}>{t("desks.strategies.expirationAuto")}</option>
              {expirations.map((x) => (
                <option key={x.date} value={x.date}>
                  {t("desks.strategies.expirationOption", { date: x.date, days: x.days })}
                </option>
              ))}
              {expiration !== AUTO && !expirations.some((x) => x.date === expiration) ? <option value={expiration}>{expiration}</option> : null}
            </Select>
          )}
        </Field>
        <Field label={t("desks.strategies.priority")}>
          {(props) => (
            <Select {...props} value={priority} onChange={(e) => setPriority(e.target.value as Priority)}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {t(`desks.strategies.priority.${p}`)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="strat-go">
          <Button type="submit" variant="primary" disabled={busy}>
            {t("desks.strategies.go")}
          </Button>
          <span className="strat-price mono" aria-label={t("desks.strategies.price")}>
            {committed ? `${committed.toUpperCase()} ${quote ? formatPrice(quote.last, locale) : "-"}` : ""}
          </span>
        </div>
      </form>
      {busy ? <Loading label={t("desks.strategies.asking")} /> : null}
      {error ? (
        <p className="strat-error" role="alert">
          {error}
        </p>
      ) : null}
      {!busy && result ? <Result result={result} desk={desk} openPanel={openPanel} /> : null}
      {!busy && !result && !error ? <p className="company-none">{t("desks.strategies.empty")}</p> : null}
    </section>
  );
}

function Result({ result, desk, openPanel }: { result: StrategiesResult; desk: TabProps["desk"]; openPanel?: TabProps["openPanel"] }) {
  const { t, locale } = useI18n();
  if (!result.ok) {
    return (
      <p className="company-none" role="status">
        {result.message ?? t("desks.strategies.noData", { source: result.source ?? "-", reason: "" })}
      </p>
    );
  }
  const notes = (result.notes ?? []).filter((n): n is string => typeof n === "string");
  return (
    <>
      <div className="strat-meta">
        <span>
          {t("desks.strategies.chain", { source: result.source ?? "-", date: result.expiration ?? "-", days: result.days ?? "-" })}
          {result.as_of ? ` · ${new Date(result.as_of).toLocaleString(locale)}` : ""}
          {result.delayed ? ` · ${t("desks.company.delayed")}` : ""}
        </span>
        {result.vol_view_source === "default_flat_uncalibrated" ? <Pill tone="warn">{t("desks.strategies.volUncalibrated")}</Pill> : null}
      </div>
      {notes.map((n) => (
        <p key={n} className="company-none">
          {n}
        </p>
      ))}
      {result.reading ? (
        <section className="company-block strat-reading" aria-label={t("desks.strategies.reading")}>
          <h3 className="lbl">{t("desks.strategies.reading")}</h3>
          <p>{result.reading}</p>
        </section>
      ) : null}
      {result.cards.length === 0 ? <p className="company-none">{t("desks.strategies.noCards")}</p> : null}
      <div className="strat-grid">
        {result.cards.map((card) => (
          <CardView key={card.id} card={card} spot={result.spot ?? null} symbol={result.symbol} expiration={result.expiration ?? null} desk={desk} openPanel={openPanel} t={t} locale={locale} />
        ))}
      </div>
    </>
  );
}

const money = (value: number | null | undefined, locale: string): string =>
  value == null || !Number.isFinite(value) ? "-" : new Intl.NumberFormat(locale, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);

interface CardProps {
  card: Card;
  spot: number | null;
  symbol: string;
  expiration: string | null;
  desk: TabProps["desk"];
  openPanel?: TabProps["openPanel"];
  t: Translate;
  locale: string;
}

function CardView({ card, spot, symbol, expiration, desk, openPanel, t, locale }: CardProps) {
  const locked = card.status === "locked";
  // A card that is closed (premium selling), does not fit the budget or needs the shares cannot be proposed: the button says why. The detail of a locked card does not open.
  const request = expiration && card.status === "ok" ? requestOfCard(symbol, expiration, card, STRIKES_OF) : null;
  const offDetail = expiration && card.can_open_detail && !locked ? requestOfCard(symbol, expiration, card, STRIKES_OF) : null;
  const openDetail = () => {
    if (!offDetail) return;
    void detailApi
      .remember(desk.id, offDetail)
      .catch(() => undefined)
      .then(() => openPanel?.("option_detail"));
  };
  const over = card.status === "over_budget";
  const verdict = card.verdict;
  const name = t(`desks.strategies.s.${card.structure}`);
  const net = card.net_premium_usd;
  return (
    <article className={`strat-card${locked ? " locked" : ""}${over ? " over" : ""}`} aria-label={name}>
      <header>
        <h3>{name}</h3>
        {verdict?.verdict ? (
          <Pill tone={TONE[verdict.verdict]}>{t(`desks.strategies.verdict.${verdict.verdict}`)}</Pill>
        ) : (
          <Pill>{t("desks.strategies.noVerdictShort")}</Pill>
        )}
      </header>
      {locked ? (
        <p className="strat-lock">
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="5" y="11" width="14" height="9" rx="2" />
            <path d="M8 11V8a4 4 0 0 1 8 0v3" />
          </svg>
          <strong>{t("desks.strategies.locked")}</strong> {card.status_message ?? verdict?.reason}
        </p>
      ) : null}
      {over ? (
        <p className="strat-over">
          <strong>{t("desks.strategies.over")}</strong> {card.status_message}
        </p>
      ) : null}
      {card.status === "needs_shares" ? <p className="strat-over">{card.status_message}</p> : null}
      <ul className="strat-legs">
        {card.legs.map((leg, i) => (
          <li key={i} className="mono">
            {t(`desks.strategies.pos.${leg.position}`)} {t(`desks.strategies.kind.${leg.kind}`)}
            {leg.strike != null ? ` ${leg.strike}` : ""}
          </li>
        ))}
      </ul>
      <Payoff card={card} spot={spot} label={t("desks.strategies.chart", { name })} />
      <table className="dterm-table company-table">
        <tbody>
          <Row label={net >= 0 ? t("desks.strategies.cost") : t("desks.strategies.credit")} value={money(Math.abs(net), locale)} />
          <Row label={t("desks.strategies.maxLoss")} value={card.unbounded_loss ? t("desks.strategies.unlimited") : money(card.max_loss_usd, locale)} />
          <Row label={t("desks.strategies.maxProfit")} value={card.unbounded_profit ? t("desks.strategies.unlimited") : money(card.max_profit_usd, locale)} />
          <Row label={t("desks.strategies.breakevens")} value={card.breakevens.length ? card.breakevens.map((b) => b.toFixed(2)).join(" / ") : "-"} />
          <Row label={t("desks.strategies.pProfit")} value={card.p_profit == null ? t("desks.strategies.noDatum") : `${(card.p_profit * 100).toFixed(1)}%`} />
          <Row label={t("desks.strategies.ev")} value={card.expected_value_usd == null ? t("desks.strategies.noDatum") : money(card.expected_value_usd, locale)} />
        </tbody>
      </table>
      {verdict && verdict.reason !== card.status_message ? (
        <p className="strat-why">
          {verdict.reason}
          {verdict.verdict && verdict.backed_by?.name ? <span className="strat-by"> {t("desks.strategies.backedBy", { name: verdict.backed_by.name })}</span> : null}
        </p>
      ) : null}
      <div className="strat-actions">
        {!locked ? (
          <Button size="sm" disabled={!offDetail || !openPanel} onClick={openDetail}>
            {t("desks.strategies.openDetail")}
          </Button>
        ) : null}
        <ProposeControls desk={desk} request={request} strategyKey={null} disabledReason={request ? null : (card.status_message ?? t("desks.strategies.cannotPropose"))} />
      </div>
    </article>
  );
}

const TONE = { recommended: "ok", possible: "acc", not_worth: "bad", closed: "warn", only_if_shares: "warn", over_budget: "warn" } as const;

function Row({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td className="r mono">{value}</td>
    </tr>
  );
}

/** Profit or loss at expiration against the price of the stock: green above zero, red below, with the price of today marked. */
function Payoff({ card, spot, label }: { card: Card; spot: number | null; label: string }) {
  const id = useId();
  const { spots, pnl_usd: pnl } = card.payoff;
  if (spots.length < 2 || spots.length !== pnl.length) return null;
  const W = 240;
  const H = 90;
  const lo = Math.min(...pnl, 0);
  const hi = Math.max(...pnl, 0);
  const span = hi - lo || 1;
  const x0 = spots[0]!;
  const xs = (spots[spots.length - 1]! - x0) || 1;
  const px = (s: number) => ((s - x0) / xs) * W;
  const py = (v: number) => 4 + (1 - (v - lo) / span) * (H - 8);
  const zero = py(0);
  const points = spots.map((s, i) => `${px(s).toFixed(1)},${py(pnl[i]!).toFixed(1)}`).join(" ");
  return (
    <svg className="strat-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} preserveAspectRatio="none">
      <defs>
        <clipPath id={`${id}-up`}>
          <rect x="0" y="0" width={W} height={zero} />
        </clipPath>
        <clipPath id={`${id}-down`}>
          <rect x="0" y={zero} width={W} height={H - zero} />
        </clipPath>
      </defs>
      <line x1="0" x2={W} y1={zero} y2={zero} className="strat-zero" />
      {spot != null && spot >= x0 && spot <= x0 + xs ? <line x1={px(spot)} x2={px(spot)} y1="0" y2={H} className="strat-spot" /> : null}
      <polyline points={points} className="strat-line up" clipPath={`url(#${id}-up)`} />
      <polyline points={points} className="strat-line down" clipPath={`url(#${id}-down)`} />
    </svg>
  );
}
