import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button, Field, Input, Loading, Pill, Select } from "@/design";
import { useI18n } from "@/i18n";
import { formatPrice } from "../../chart/format";
import { useQuotes } from "../AssetList";
import { failText } from "../configApi";
import { detailApi, DEBOUNCE_MS, movedEnough, PRINCIPAL, STRIKES_OF, requestOfCard } from "../optionDetailApi";
import type { DetailRequest, DetailResult, EventBlock, SavedStrategy, Selection } from "../optionDetailApi";
import { ProposeControls } from "../ProposeControls";
import { strategiesApi } from "../strategiesApi";
import type { TabProps } from "./types";

const SYMBOL = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/;
const FALLBACK = { range: { min: 3, max: 20, default: 10 }, iv: { min: 0.5, max: 2, default: 1 } };
const CURRENT = "";

const money = (value: number | null | undefined, locale: string): string =>
  value == null || !Number.isFinite(value) ? "-" : new Intl.NumberFormat(locale, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);

const sameRequest = (a: DetailRequest, b: DetailRequest): boolean =>
  a.symbol === b.symbol && a.expiration === b.expiration && a.structure === b.structure && a.quantity === b.quantity && JSON.stringify(a.strikes) === JSON.stringify(b.strikes) &&
  JSON.stringify(a.legs) === JSON.stringify(b.legs);

/**
 * Option detail of a desk (SPEC-028, delivery 6): one strategy on the real chain, with its profit and loss by date and price, its chart at expiration and at
 * half of the term, the breakevens, the events before expiration and the two sliders (price range, volatility). It asks the server when the owner releases a
 * slider (after a short wait) and when the live price moves more than the percent the server sends; never on a clock. "Save" keeps the legs under a
 * strategy_key of the desk; "Propose" sends it to the door of execution, which never sends by itself.
 */
export function OptionDetailTab({ desk }: TabProps) {
  const { t, locale } = useI18n();
  const [request, setRequest] = useState<DetailRequest | null>(null);
  const [strategyKey, setStrategyKey] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [range, setRange] = useState(FALLBACK.range.default);
  const [iv, setIv] = useState(FALLBACK.iv.default);
  const [committed, setCommitted] = useState({ range: FALLBACK.range.default, iv: FALLBACK.iv.default });
  const [result, setResult] = useState<DetailResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [saved, setSaved] = useState<SavedStrategy[]>([]);
  const [fromCards, setFromCards] = useState<Array<{ id: string; label: string; request: DetailRequest }>>([]);
  const [saving, setSaving] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const turn = useRef(0);
  const held = useRef<DetailResult | null>(null);
  held.current = result;

  // The panel opens with the strategy of the desk: the one the owner opened from Strategies, the chief opened by chat, or the owner left last.
  useEffect(() => {
    let alive = true;
    const last = detailApi.selection(desk.id).catch(() => null as Selection | null);
    const list = detailApi.saved(desk.id).catch(() => [] as SavedStrategy[]);
    const cards = strategiesApi.last(desk.id).catch(() => null);
    void Promise.all([last, list, cards]).then(([sel, savedList, consult]) => {
      if (!alive) return;
      setSaved(savedList);
      const result = consult?.result;
      if (result?.ok && result.expiration) {
        setFromCards(
          result.cards
            .filter((c) => c.status === "ok" && c.can_open_detail)
            .map((c) => ({ id: c.id, label: `${result.symbol} ${t(`desks.strategies.s.${c.structure}`)} ${result.expiration}`, request: requestOfCard(result.symbol, result.expiration as string, c, STRIKES_OF) })),
        );
      }
      if (sel) {
        setRequest({ symbol: sel.symbol, expiration: sel.expiration, structure: sel.structure, strikes: sel.strikes, legs: sel.legs, quantity: sel.quantity });
        setStrategyKey(sel.strategy_key);
        setName(sel.name);
        const r = sel.range_pct ?? FALLBACK.range.default;
        const v = sel.iv_mult ?? FALLBACK.iv.default;
        setRange(r);
        setIv(v);
        setCommitted({ range: r, iv: v });
      }
      setReady(true);
    });
    return () => {
      alive = false;
    };
    // The selection is read when the desk opens; `t` only labels the cards.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desk.id]);

  const ask = useCallback(
    (req: DetailRequest, limits: { range: number; iv: number }) => {
      const mine = ++turn.current;
      setBusy(true);
      setError(null);
      detailApi
        .ask(desk.id, { ...req, range_pct: limits.range, iv_mult: limits.iv })
        .then((data) => mine === turn.current && setResult(data))
        .catch((cause) => mine === turn.current && setError(failText(cause, t("desks.detail.error"))))
        .finally(() => mine === turn.current && setBusy(false));
    },
    [desk.id, t],
  );

  // A strategy chosen, the contracts changed or a slider released: one request, after the wait of the server for the sliders.
  const wait = result?.recalc?.debounce_ms ?? DEBOUNCE_MS;
  const requestKey = request ? JSON.stringify(request) : "";
  useEffect(() => {
    if (!request) return;
    const timer = setTimeout(() => ask(request, committed), wait);
    return () => clearTimeout(timer);
    // `requestKey` stands for the strategy; `wait` is read once per change of it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey, committed.range, committed.iv]);

  // The price follows the stream. When it moves more than the percent of the answer the panel holds, the table is asked again: an event, not a timer.
  const symbol = request?.symbol ?? "";
  const quotes = useQuotes(SYMBOL.test(symbol) ? [symbol.toUpperCase()] : []);
  const state = quotes[symbol.toUpperCase()];
  const live = state && state !== "failed" ? state.last : null;
  useEffect(() => {
    const now = held.current;
    if (!request || !now?.ok || !now.recalc) return;
    if (movedEnough(now.recalc.anchor_spot, live, now.recalc.price_move_pct)) ask(request, committed);
    // Only a new price of the stream can trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  const choose = (next: DetailRequest, key: string | null, label: string | null) => {
    setRequest(next);
    setStrategyKey(key);
    setName(label);
    setResult(null);
    setSaveMessage(null);
    void detailApi.remember(desk.id, { ...next, range_pct: committed.range, iv_mult: committed.iv, strategy_key: key, name: label }).catch(() => undefined);
  };

  const pick = (value: string) => {
    if (value === CURRENT) return;
    const [kind, id] = [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)];
    if (kind === "saved") {
      const found = saved.find((s) => s.key === id);
      if (found) choose({ symbol: found.symbol, expiration: found.expiration, structure: found.structure, strikes: found.strikes, legs: found.legs, quantity: found.quantity }, found.key, found.name);
    } else {
      const found = fromCards.find((c) => c.id === id);
      if (found) choose(found.request, null, null);
    }
  };

  const setQuantity = (text: string) => {
    const n = Number(text);
    if (!request || !Number.isInteger(n) || n < 1 || n > 1000) return;
    const next = { ...request, quantity: n };
    if (!sameRequest(request, next)) choose(next, strategyKey, name);
  };

  const save = async () => {
    if (!request || saveName.trim() === "") return;
    try {
      const done = await detailApi.save(desk.id, saveName.trim(), request);
      setSaved(await detailApi.saved(desk.id));
      setStrategyKey(done.key);
      setName(done.name);
      setSaving(false);
      setSaveName("");
      setSaveMessage(t("desks.detail.saved", { name: done.name }));
      void detailApi.remember(desk.id, { ...request, range_pct: committed.range, iv_mult: committed.iv, strategy_key: done.key, name: done.name }).catch(() => undefined);
    } catch (cause) {
      setSaveMessage(failText(cause, t("desks.detail.saveError")));
    }
  };

  const limits = result?.limits ?? { range_pct: { min: FALLBACK.range.min, max: FALLBACK.range.max, default: FALLBACK.range.default }, iv_mult: { min: FALLBACK.iv.min, max: FALLBACK.iv.max, default: FALLBACK.iv.default } };
  const release = () => setCommitted((now) => (now.range === range && now.iv === iv ? now : { range, iv }));

  if (!ready) return <Loading label={t("desks.detail.loading")} />;
  return (
    <section className="opd-tab" aria-label={t("desks.tab.option_detail")}>
      <div className="opd-top">
        <Field label={t("desks.detail.selector")}>
          {(props) => (
            <Select {...props} value={CURRENT} onChange={(e) => pick(e.target.value)}>
              <option value={CURRENT}>{request ? `${request.symbol} ${request.structure ? t(`desks.strategies.s.${request.structure}`) : t("desks.detail.custom")} ${request.expiration}${name ? ` (${name})` : ""}` : t("desks.detail.none")}</option>
              {saved.length > 0 ? (
                <optgroup label={t("desks.detail.savedGroup")}>
                  {saved.map((s) => (
                    <option key={s.key} value={`saved:${s.key}`}>
                      {s.name}
                    </option>
                  ))}
                </optgroup>
              ) : null}
              {fromCards.length > 0 ? (
                <optgroup label={t("desks.detail.cardsGroup")}>
                  {fromCards.map((c) => (
                    <option key={c.id} value={`card:${c.id}`}>
                      {c.label}
                    </option>
                  ))}
                </optgroup>
              ) : null}
            </Select>
          )}
        </Field>
        {request ? (
          <Field label={t("desks.detail.quantity")}>
            {(props) => <Input {...props} key={requestKey} className="mono" inputMode="numeric" defaultValue={String(request.quantity)} onBlur={(e) => setQuantity(e.target.value)} onKeyDown={(e) => e.key === "Enter" && setQuantity(e.currentTarget.value)} />}
          </Field>
        ) : null}
        <span className="strat-price mono" aria-label={t("desks.strategies.price")}>
          {request ? `${request.symbol.toUpperCase()} ${live != null ? formatPrice(live, locale) : "-"}` : ""}
        </span>
      </div>
      {!request ? <p className="company-none">{t("desks.detail.empty")}</p> : null}
      {busy && !result ? <Loading label={t("desks.detail.asking")} /> : null}
      {error ? (
        <p className="strat-error" role="alert">
          {error}
        </p>
      ) : null}
      {result && !result.ok ? (
        <p className="company-none" role="status">
          {result.message}
        </p>
      ) : null}
      {request && result?.ok ? (
        <Detail
          result={result}
          live={live}
          busy={busy}
          range={range}
          iv={iv}
          limits={limits}
          onRange={setRange}
          onIv={setIv}
          onRelease={release}
          locale={locale}
        />
      ) : null}
      {request ? (
        <div className="opd-actions">
          {saving ? (
            <form
              className="opd-save"
              onSubmit={(e) => {
                e.preventDefault();
                void save();
              }}
            >
              <Field label={t("desks.detail.saveName")}>{(props) => <Input {...props} value={saveName} maxLength={60} autoFocus onChange={(e) => setSaveName(e.target.value)} />}</Field>
              <Button type="submit" size="sm" variant="primary" disabled={saveName.trim() === ""}>
                {t("desks.detail.saveConfirm")}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setSaving(false)}>
                {t("desks.accounts.cancel")}
              </Button>
            </form>
          ) : (
            <Button size="sm" onClick={() => setSaving(true)}>
              {t("desks.detail.save")}
            </Button>
          )}
          <ProposeControls desk={desk} request={request} strategyKey={strategyKey ?? PRINCIPAL} />
          {saveMessage ? (
            <p className="company-none" role="status">
              {saveMessage}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

interface DetailProps {
  result: DetailResult;
  live: number | null;
  busy: boolean;
  range: number;
  iv: number;
  limits: NonNullable<DetailResult["limits"]>;
  onRange: (n: number) => void;
  onIv: (n: number) => void;
  onRelease: () => void;
  locale: string;
}

function Detail({ result, live, busy, range, iv, limits, onRange, onIv, onRelease, locale }: DetailProps) {
  const { t } = useI18n();
  const metrics = result.metrics!;
  const table = result.table!;
  const curves = result.curves!;
  const events = result.events!;
  const legs = result.legs ?? [];
  const debit = metrics.premium_type === "debit";
  const slide = { onPointerUp: onRelease, onKeyUp: onRelease, onBlur: onRelease };
  return (
    <>
      <div className="strat-meta">
        <span>
          {t("desks.detail.chain", { source: result.source ?? "-", date: result.expiration, days: result.days ?? "-" })}
          {result.as_of ? ` · ${new Date(result.as_of).toLocaleString(locale)}` : ""}
          {result.delayed ? ` · ${t("desks.company.delayed")}` : ""}
        </span>
        {busy ? <Pill>{t("desks.detail.updating")}</Pill> : null}
      </div>
      <ul className="strat-legs" aria-label={t("desks.detail.legs")}>
        {legs.map((leg, i) => (
          <li key={i} className="mono">
            {leg.instrument === "stock"
              ? `${t("desks.strategies.pos.long")} ${t("desks.strategies.kind.stock")} ${leg.quantity}`
              : `${t(`desks.strategies.pos.${leg.position}`)} ${t(`desks.strategies.kind.${leg.kind}`)} ${leg.strike} @ ${leg.premium.toFixed(2)}${leg.iv_used != null ? ` · IV ${(leg.iv_used * 100).toFixed(1)}%` : ""}`}
          </li>
        ))}
      </ul>
      <table className="dterm-table company-table opd-metrics">
        <tbody>
          <Row label={debit ? t("desks.detail.debit") : t("desks.detail.credit")} value={money(Math.abs(metrics.net_premium_usd), locale)} />
          <Row label={t("desks.strategies.maxLoss")} value={metrics.unbounded_loss ? t("desks.strategies.unlimited") : money(metrics.max_loss_usd, locale)} />
          <Row label={t("desks.strategies.maxProfit")} value={metrics.unbounded_profit ? t("desks.strategies.unlimited") : money(metrics.max_profit_usd, locale)} />
          <Row
            label={metrics.breakevens.length > 1 ? t("desks.detail.breakevens") : t("desks.detail.breakeven")}
            value={metrics.breakevens.length ? metrics.breakevens.map((b) => b.toFixed(2)).join(" / ") : "-"}
          />
          <Row label={t("desks.strategies.pProfit")} value={metrics.p_profit == null ? t("desks.strategies.noDatum") : `${(metrics.p_profit * 100).toFixed(1)}%`} />
          <Row label={t("desks.strategies.ev")} value={metrics.expected_value_usd == null ? t("desks.strategies.noDatum") : money(metrics.expected_value_usd, locale)} />
        </tbody>
      </table>
      <div className="opd-events" role="group" aria-label={t("desks.detail.events")}>
        <EventLine label={t("desks.detail.earnings")} event={events.earnings} t={t} />
        <EventLine label={t("desks.detail.exDividend")} event={events.ex_dividend} t={t} />
      </div>
      <div className="opd-sliders">
        <label className="opd-slider">
          <span>{t("desks.detail.range", { n: range })}</span>
          <input type="range" min={limits.range_pct.min} max={limits.range_pct.max} step={1} value={range} aria-label={t("desks.detail.rangeLabel")} onChange={(e) => onRange(Number(e.target.value))} {...slide} />
        </label>
        <label className="opd-slider">
          <span>{t("desks.detail.iv", { n: iv.toFixed(2) })}</span>
          <input type="range" min={limits.iv_mult.min} max={limits.iv_mult.max} step={0.05} value={iv} aria-label={t("desks.detail.ivLabel")} onChange={(e) => onIv(Number(e.target.value))} {...slide} />
        </label>
      </div>
      <Curves curves={curves} spot={result.spot ?? null} live={live} t={t} />
      <Grid table={table} locale={locale} t={t} />
      {(result.warnings ?? []).map((w) => (
        <p key={w} className="company-none">
          {w}
        </p>
      ))}
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td className="r mono">{value}</td>
    </tr>
  );
}

type Translate = ReturnType<typeof useI18n>["t"];

function EventLine({ label, event, t }: { label: string; event: EventBlock; t: Translate }) {
  const before = event.relation === "before_expiration";
  const none = event.relation === "no_data";
  return (
    <p className={`opd-event${before ? " warn" : ""}${none ? " nodata" : ""}`}>
      <strong>{label}</strong> {before ? <Pill tone="warn">{t("desks.detail.beforeExpiration")}</Pill> : null} {event.message}
    </p>
  );
}

/** The price rows against the date columns: green above zero, red below, the row of the current price marked. A cell the model cannot price is "-". */
function Grid({ table, locale, t }: { table: NonNullable<DetailResult["table"]>; locale: string; t: Translate }) {
  return (
    <div className="opd-grid-wrap">
      <table className="dterm-table company-table opd-grid" aria-label={t("desks.detail.grid")}>
        <thead>
          <tr>
            <th scope="col">{t("desks.detail.price")}</th>
            {table.columns.map((c) => (
              <th key={c.date + c.days} scope="col" className="r">
                {c.date}
                {c.expiration ? ` · ${t("desks.detail.atExpiration")}` : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, i) => (
            <tr key={row.pct} className={row.current ? "opd-current" : undefined} aria-current={row.current ? "true" : undefined}>
              <th scope="row" className="mono">
                {row.price.toFixed(2)} <span className="opd-pct">{row.pct > 0 ? "+" : ""}{row.pct.toFixed(1)}%</span>
              </th>
              {table.pnl_usd[i]!.map((v, j) => (
                <td key={j} className={`r mono ${v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""}`}>
                  {v == null ? "-" : v.toLocaleString(locale, { maximumFractionDigits: 0 })}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Profit or loss against the price: the line at expiration (solid, green above zero and red below) and the line at half of the term (dashed), the price of today and the breakevens. */
function Curves({ curves, spot, live, t }: { curves: NonNullable<DetailResult["curves"]>; spot: number | null; live: number | null; t: Translate }) {
  const id = useId();
  const { spots, expiration_pnl_usd: exp, mid_pnl_usd: mid } = curves;
  if (spots.length < 2 || spots.length !== exp.length) return null;
  const W = 320;
  const H = 150;
  const all = [...exp, ...(mid ?? []), 0];
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const span = hi - lo || 1;
  const x0 = spots[0]!;
  const xs = spots[spots.length - 1]! - x0 || 1;
  const px = (s: number) => ((s - x0) / xs) * W;
  const py = (v: number) => 6 + (1 - (v - lo) / span) * (H - 12);
  const zero = py(0);
  const line = (ys: number[]) => spots.map((s, i) => `${px(s).toFixed(1)},${py(ys[i]!).toFixed(1)}`).join(" ");
  const marker = live ?? spot;
  const mids = curves.mid_breakevens ?? [];
  return (
    <figure className="opd-chart">
      <svg className="opd-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t("desks.detail.chart")} preserveAspectRatio="none">
        <defs>
          <clipPath id={`${id}-up`}>
            <rect x="0" y="0" width={W} height={zero} />
          </clipPath>
          <clipPath id={`${id}-down`}>
            <rect x="0" y={zero} width={W} height={H - zero} />
          </clipPath>
        </defs>
        <line x1="0" x2={W} y1={zero} y2={zero} className="strat-zero" />
        {marker != null && marker >= x0 && marker <= x0 + xs ? <line x1={px(marker)} x2={px(marker)} y1="0" y2={H} className="strat-spot" /> : null}
        {mid ? <polyline points={line(mid)} className="opd-mid" /> : null}
        <polyline points={line(exp)} className="strat-line up" clipPath={`url(#${id}-up)`} />
        <polyline points={line(exp)} className="strat-line down" clipPath={`url(#${id}-down)`} />
        {curves.mid_breakevens ? mids.map((b) => <circle key={b} cx={px(b)} cy={zero} r="3" className="opd-be approx" />) : null}
      </svg>
      <figcaption className="opd-legend">
        <span className="opd-key exp">{t("desks.detail.atExpirationLine")}</span>
        {mid ? <span className="opd-key mid">{t("desks.detail.halfTermLine", { days: Math.round(curves.mid_days) })}</span> : <span className="opd-key">{t("desks.detail.noMidLine")}</span>}
        {curves.mid_breakevens ? (
          <span className="opd-key approx">
            {mids.length ? t("desks.detail.midBreakevens", { values: mids.map((b) => b.toFixed(2)).join(" / ") }) : t("desks.detail.midBreakevensNone")} {t("desks.detail.approximate")}
          </span>
        ) : null}
      </figcaption>
    </figure>
  );
}
