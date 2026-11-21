import { Fragment, useEffect, useRef, useState } from "react";
import { Button, Loading } from "@/design";
import { useI18n } from "@/i18n";
import { requestFocus } from "@/screens/connections/focus";
import { useShell } from "@/shell/useShell";
import { COLUMN_KEY, COLUMN_SETS, COLUMN_TITLE, DEFAULT_COLUMN_SET } from "./columns";
import type { ColumnId, ColumnSetId } from "./columns";
import { FILTER_OFF, visibleGroups, visibleRows } from "./filter";
import type { FilterState, FilterValues } from "./filter";
import { FilterPanel } from "./FilterPanel";
import { decimal, groupDate, percent, priced, quoteMoment, whole } from "./format";
import { optionsErrorText } from "./optionsApi";
import { DEFAULT_STRIKES, STRIKE_CHOICES } from "./types";
import type { ExpirationGroup, ExpirationItem, OptionCell, PriceFeed, StrikeChoice } from "./types";
import { UnderlyingBar } from "./UnderlyingBar";
import { useOptionChain } from "./useOptionChain";
import type { GroupState } from "./useOptionChain";
import "./options.css";

export const COLUMN_SET_KEY = "hokma.options.columnSet";
export const STRIKES_KEY = "hokma.options.strikes";

function readStored<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const stored = localStorage.getItem(key);
    return allowed.includes(stored as T) ? (stored as T) : fallback;
  } catch {
    return fallback;
  }
}
function store(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts until the page closes */
  }
}

type Side = "call" | "put";

/** The line under the bar for each price feed. Schwab sends its own greeks; Hokma calculates only Prob.ITM. */
const FEED_KEY: Record<PriceFeed, string> = {
  indicative: "options.feed.indicative",
  opra: "options.feed.opra",
  schwab: "options.feed.schwab",
};

interface Tr {
  t: (key: string, params?: Record<string, string | number>) => string;
  locale: string;
}

/** The text of one cell. A missing value is "N/A"; the exchange letter goes after the price only when it came. */
function cellText(column: ColumnId, cell: OptionCell, { t, locale }: Tr): string {
  switch (column) {
    case "last":
      return priced(cell.last, cell.last_exchange, locale, t);
    case "bid":
      return priced(cell.bid, cell.bid_exchange, locale, t);
    case "ask":
      return priced(cell.ask, cell.ask_exchange, locale, t);
    case "probItm":
      return percent(cell.prob_itm, locale, t);
    case "intrinsic":
      return decimal(cell.intrinsic, 2, locale, t);
    case "openInterest":
      return whole(cell.open_interest, locale, t);
    case "volume":
      return whole(cell.volume, locale, t);
    case "theta":
      return decimal(cell.theta, 2, locale, t);
    case "delta":
      return decimal(cell.delta, 2, locale, t);
    case "iv":
      return percent(cell.iv, locale, t);
  }
}

function groupSummary(group: ExpirationGroup | undefined, { t, locale }: Tr): string {
  if (!group) return "";
  if (group.iv === null && group.expected_move === null) return t("options.missing");
  return t("options.group.ivMove", {
    iv: percent(group.iv, locale, t),
    move: decimal(group.expected_move, 3, locale, t),
  });
}

export function OptionChainView({ symbol }: { symbol: string }) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const tr: Tr = { t, locale };
  const [columnSet, setColumnSet] = useState<ColumnSetId>(() => readStored<ColumnSetId>(COLUMN_SET_KEY, ["1", "2"], DEFAULT_COLUMN_SET));
  const [strikes, setStrikes] = useState<StrikeChoice>(() => readStored<StrikeChoice>(STRIKES_KEY, STRIKE_CHOICES, DEFAULT_STRIKES));
  const [filter, setFilter] = useState<FilterState>(FILTER_OFF);
  const [panel, setPanel] = useState(false);
  const { state, toggle, reload } = useOptionChain(symbol, strikes);

  // Another number of strikes asks again for the open groups. Not on mount: the first load already used it.
  const asked = useRef(strikes);
  useEffect(() => {
    if (asked.current === strikes) return;
    asked.current = strikes;
    reload(false);
  }, [strikes, reload]);

  const columns = COLUMN_SETS[columnSet];
  const span = columns.calls.length + columns.puts.length + 2;
  const items = visibleGroups(state.expirations.items, filter);
  const meta = state.meta;
  const feedText = meta ? t(FEED_KEY[meta.price_feed] ?? "options.feed.indicative") : "";
  const closedText = meta && !meta.market_open ? t("options.closed", { when: quoteMoment(meta.as_of, locale) }) : "";
  const failure = state.expirations.status === "error" ? (state.expirations.error ?? "upstream") : null;

  const openConnections = () => {
    requestFocus("equity");
    shell.openScreen("connections");
  };

  const head = (column: ColumnId) => (
    <th key={column} scope="col" title={COLUMN_TITLE[column] ? t(COLUMN_TITLE[column]!) : undefined} data-col={column}>
      {t(COLUMN_KEY[column])}
    </th>
  );

  return (
    <div className="opt" data-testid="options-view">
      <UnderlyingBar bar={state.underlying} />
      <section className="opt-chain" aria-label={t("options.chain.title")}>
        <div className="opt-bar">
          <div className="opt-pop-anchor">
            <Button
              size="sm"
              aria-haspopup="dialog"
              aria-expanded={panel}
              data-testid="options-filter"
              onClick={() => setPanel((v) => !v)}
            >
              {t("options.filter", { state: t(filter.on ? "options.filter.on" : "options.filter.off") })} <span aria-hidden="true">▾</span>
            </Button>
            {panel ? (
              <div className="opt-pop" role="dialog" aria-label={t("options.filter", { state: t(filter.on ? "options.filter.on" : "options.filter.off") })}>
                <FilterPanel
                  values={filter.values}
                  active={filter.on}
                  onApply={(values: FilterValues) => {
                    setFilter({ on: true, values });
                    setPanel(false);
                  }}
                  onClear={() => {
                    setFilter((f) => ({ on: false, values: f.values }));
                    setPanel(false);
                  }}
                />
              </div>
            ) : null}
          </div>
          <span className="opt-fixed">{t("options.spread")}</span>
          <select
            className="opt-select"
            aria-label={t("options.columns")}
            data-testid="options-columns"
            value={columnSet}
            onChange={(event) => {
              const next = event.target.value as ColumnSetId;
              setColumnSet(next);
              store(COLUMN_SET_KEY, next);
            }}
          >
            <option value="1">{t("options.columns.set1")}</option>
            <option value="2">{t("options.columns.set2")}</option>
          </select>
          <label className="opt-strikes">
            <span>{t("options.strikes")}</span>
            <select
              className="opt-select"
              data-testid="options-strikes"
              value={strikes}
              onChange={(event) => {
                const next = event.target.value as StrikeChoice;
                setStrikes(next);
                store(STRIKES_KEY, next);
              }}
            >
              {STRIKE_CHOICES.map((choice) => (
                <option key={choice} value={choice}>
                  {choice === "all" ? t("options.strikes.all") : choice}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" data-testid="options-refresh" style={{ marginLeft: "auto" }} onClick={() => reload(true)}>
            {t("options.refresh")}
          </Button>
        </div>
        <div className="opt-status">
          <span data-testid="options-feed">{feedText}</span>
          <span data-testid="options-closed">{closedText}</span>
        </div>

        {state.expirations.status === "loading" ? (
          <div className="opt-center">
            <Loading label={t("options.loading")} />
          </div>
        ) : failure === "no_connection" ? (
          <div className="opt-center" data-testid="options-no-connection">
            <p className="p">{t("options.noConnection.any")}</p>
            <Button size="sm" variant="primary" onClick={openConnections}>
              {t("options.openConnections")}
            </Button>
          </div>
        ) : failure ? (
          <div className="opt-center" role="alert" data-testid="options-error" data-code={failure}>
            <p className="p">{optionsErrorText(failure)}</p>
          </div>
        ) : (
          <div className="opt-scroll">
            <table className="opt-table">
              <thead>
                <tr className="opt-sides">
                  <th colSpan={columns.calls.length} scope="colgroup">
                    {t("options.side.calls")}
                  </th>
                  <th colSpan={2} aria-hidden="true" />
                  <th colSpan={columns.puts.length} scope="colgroup">
                    {t("options.side.puts")}
                  </th>
                </tr>
                <tr>
                  {columns.calls.map(head)}
                  <th scope="col">{t("options.col.exp")}</th>
                  <th scope="col">{t("options.col.strike")}</th>
                  {columns.puts.map(head)}
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <GroupRows
                    key={item.date}
                    item={item}
                    group={state.groups[item.date]}
                    open={state.open.has(item.date)}
                    filter={filter}
                    columns={columns}
                    span={span}
                    symbol={symbol}
                    tr={tr}
                    onToggle={() => toggle(item.date)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

interface GroupProps {
  item: ExpirationItem;
  group: GroupState | undefined;
  open: boolean;
  filter: FilterState;
  columns: { calls: readonly ColumnId[]; puts: readonly ColumnId[] };
  span: number;
  symbol: string;
  tr: Tr;
  onToggle: () => void;
}

function GroupRows({ item, group, open, filter, columns, span, symbol, tr, onToggle }: GroupProps) {
  const { t, locale } = tr;
  const when = groupDate(item.date, locale);
  const rows = group?.data ? visibleRows(group.data, filter) : [];
  const cell = (column: ColumnId, value: OptionCell | null, side: Side) => (
    <td key={side + column} className={"mono num" + (value?.itm ? " opt-itm" : "")} data-col={column} data-side={side}>
      {value ? cellText(column, value, tr) : ""}
    </td>
  );
  return (
    <Fragment>
      <tr className="opt-group" data-testid={"options-group-" + item.date}>
        <td colSpan={span}>
          <div className="opt-group-bar">
            <button type="button" className="opt-fold" aria-expanded={open} onClick={onToggle}>
              <span aria-hidden="true">{open ? "▾" : "▸"}</span>{" "}
              {t("options.group.header", { date: when, days: item.days, multiplier: item.multiplier })}
            </button>
            <span className="mono" data-testid="options-group-summary">{groupSummary(group?.data, tr)}</span>
          </div>
        </td>
      </tr>
      {open && (!group || (group.status === "loading" && !group.data)) ? (
        <tr className="opt-note">
          <td colSpan={span}>{t("options.loading")}</td>
        </tr>
      ) : null}
      {open && group?.status === "error" ? (
        <tr className="opt-note" role="alert" data-code={group.error}>
          <td colSpan={span}>{optionsErrorText(group.error ?? "upstream")}</td>
        </tr>
      ) : null}
      {open && group?.data && group.status !== "error" && group.data.rows.length === 0 ? (
        <tr className="opt-note">
          <td colSpan={span}>{t("options.empty", { symbol })}</td>
        </tr>
      ) : null}
      {open && group?.data && group.status !== "error"
        ? rows.map((row) => (
            <tr key={row.strike} className="opt-row" data-strike={row.strike}>
              {columns.calls.map((column) => cell(column, row.call, "call"))}
              <td className="mono num opt-center-col">{when}</td>
              <td className="mono num opt-center-col opt-strike">{decimal(row.strike, row.strike % 1 === 0 ? 0 : 2, locale, t)}</td>
              {columns.puts.map((column) => cell(column, row.put, "put"))}
            </tr>
          ))
        : null}
    </Fragment>
  );
}
