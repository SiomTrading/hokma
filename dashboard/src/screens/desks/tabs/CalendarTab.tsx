import { useCallback, useEffect, useRef, useState } from "react";
import { Button, EmptyState, ErrorState, Loading, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { formatPercent } from "../../chart/format";
import { calendarApi } from "../calendarApi";
import type { CalendarDay, CalendarFilter, CalendarRow, CalendarWeek } from "../calendarApi";
import { failText } from "../configApi";
import type { TabProps } from "./types";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; week: CalendarWeek };

const FILTERS: readonly CalendarFilter[] = ["all", "list", "liquid"];
const HOURS = ["bmo", "amc", "dmh"];

/** A day as YYYY-MM-DD, from its parts: no time zone is involved. */
const iso = (year: number, month: number, day: number): string => new Date(Date.UTC(year, month, day)).toISOString().slice(0, 10);

/** The Monday of the week of `day` (YYYY-MM-DD). */
export function mondayOf(day: string): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  const weekday = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  return iso(y, m - 1, d - ((weekday + 6) % 7));
}

/** `day` moved `days` days. */
export function addDays(day: string, days: number): string {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  return iso(y, m - 1, d + days);
}

const localToday = (): string => {
  const now = new Date();
  return iso(now.getFullYear(), now.getMonth(), now.getDate());
};

const dateOf = (value: string, locale: string, options: Intl.DateTimeFormatOptions): string =>
  new Intl.DateTimeFormat(locale, { timeZone: "UTC", ...options }).format(new Date(`${value}T12:00:00Z`));

/**
 * Calendar of earnings of a desk (SPEC-028, delivery 7): the week from Monday to Friday, who reports each day, when, the estimate and, once
 * published, the real number and the surprise. The week is read when the panel opens or the week or the filter change, never on a timer.
 * A click on a ticker opens the Company panel with it.
 */
export function CalendarTab({ desk, onOpenCompany }: TabProps) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const [monday, setMonday] = useState(() => mondayOf(localToday()));
  const [filter, setFilter] = useState<CalendarFilter>("all");
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const latest = useRef(0);

  const read = useCallback(() => {
    const turn = ++latest.current;
    setLoad({ state: "loading" });
    calendarApi
      .week(desk.id, monday, addDays(monday, 4), filter)
      .then((week) => turn === latest.current && setLoad({ state: "ready", week }))
      .catch((cause) => turn === latest.current && setLoad({ state: "error", message: failText(cause, t("desks.calendar.error")) }));
  }, [desk.id, monday, filter, t]);

  useEffect(read, [read]);

  const thisWeek = mondayOf(localToday());
  const range = `${dateOf(monday, locale, { day: "numeric", month: "short" })} - ${dateOf(addDays(monday, 4), locale, { day: "numeric", month: "short", year: "numeric" })}`;

  return (
    <section className="calendar-tab" aria-label={t("desks.tab.calendar")}>
      <div className="calendar-bar">
        <Button size="sm" aria-label={t("desks.calendar.prev")} onClick={() => setMonday(addDays(monday, -7))}>
          {"<"}
        </Button>
        <span className="calendar-range" aria-live="polite">
          {range}
        </span>
        <Button size="sm" aria-label={t("desks.calendar.next")} onClick={() => setMonday(addDays(monday, 7))}>
          {">"}
        </Button>
        <Button size="sm" disabled={monday === thisWeek} onClick={() => setMonday(thisWeek)}>
          {t("desks.calendar.thisWeek")}
        </Button>
        <Segmented
          label={t("desks.calendar.filter")}
          value={filter}
          onChange={setFilter}
          options={FILTERS.map((value) => ({ value, label: t(`desks.calendar.filter.${value}`) }))}
        />
      </div>
      {load.state === "loading" ? <Loading label={t("desks.calendar.loading")} /> : null}
      {load.state === "error" ? <ErrorState message={load.message} onRetry={read} /> : null}
      {load.state === "ready" && !load.week.connected ? (
        <EmptyState
          icon="connections"
          title={t("desks.calendar.connect.title")}
          description={t("desks.calendar.connect.text")}
          action={
            <Button variant="primary" size="sm" onClick={() => shell.openScreen("connections")}>
              {t("desks.calendar.connect.go")}
            </Button>
          }
        />
      ) : null}
      {load.state === "ready" && load.week.connected ? <Week week={load.week} onOpen={onOpenCompany} /> : null}
    </section>
  );
}

function Week({ week, onOpen }: { week: CalendarWeek; onOpen?: (symbol: string) => void }) {
  const { t } = useI18n();
  return (
    <>
      <p className="calendar-notes">
        {week.source ? <span>{t("desks.calendar.source", { source: week.source })}</span> : null}
        {!week.holidays_known ? <span>{t("desks.calendar.noHolidays")}</span> : null}
        {week.liquid ? <span>{t("desks.calendar.liquidNote", { n: String(week.liquid.checked), m: String(week.liquid.unchecked) })}</span> : null}
      </p>
      <div className="calendar-week">
        {week.days.map((day) => (
          <Day key={day.date} day={day} today={week.today} filter={week.filter} onOpen={onOpen} />
        ))}
      </div>
    </>
  );
}

function Day({ day, today, filter, onOpen }: { day: CalendarDay; today: string; filter: CalendarFilter; onOpen?: (symbol: string) => void }) {
  const { t, locale } = useI18n();
  const isToday = day.date === today;
  return (
    <section className={`calendar-day${isToday ? " today" : ""}${day.holiday ? " holiday" : ""}`} aria-label={dateOf(day.date, locale, { weekday: "long", day: "numeric", month: "long" })}>
      <h3 className="calendar-day-hd">
        <span>{dateOf(day.date, locale, { weekday: "short", day: "numeric" })}</span>
        {isToday ? <span className="calendar-tag">{t("desks.calendar.today")}</span> : null}
        {day.holiday ? <span className="calendar-tag">{t("desks.calendar.holiday")}</span> : null}
      </h3>
      {day.error ? <p className="company-none" title={day.error}>{t("desks.calendar.dayError")}</p> : null}
      {!day.error && day.rows.length === 0 ? <p className="company-none">{t(filter === "all" ? "desks.calendar.noReports" : "desks.calendar.noMatches")}</p> : null}
      {day.rows.length > 0 ? (
        <table className="dterm-table calendar-table">
          <thead>
            <tr>
              <th scope="col">{t("desks.calendar.ticker")}</th>
              <th scope="col" className="r">{t("desks.calendar.est")}</th>
              <th scope="col" className="r">{t("desks.calendar.actual")}</th>
            </tr>
          </thead>
          <tbody>
            {day.rows.map((row) => (
              <Report key={`${row.symbol}-${row.date}`} row={row} onOpen={onOpen} />
            ))}
          </tbody>
        </table>
      ) : null}
      {day.truncated ? <p className="company-none">{t("desks.calendar.truncated")}</p> : null}
    </section>
  );
}

function Report({ row, onOpen }: { row: CalendarRow; onOpen?: (symbol: string) => void }) {
  const { t, locale } = useI18n();
  const num = (value: number | null) => (value == null ? "-" : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value));
  const tone = row.surprise_pct == null ? "" : row.surprise_pct >= 0 ? "up" : "down";
  return (
    <tr>
      <td>
        <button type="button" className="calendar-ticker" aria-label={t("desks.calendar.openCompany", { symbol: row.symbol })} onClick={() => onOpen?.(row.symbol)}>
          {row.symbol}
        </button>
        {row.hour && HOURS.includes(row.hour) ? <span className="calendar-hour">{t(`desks.calendar.hour.${row.hour}`)}</span> : null}
      </td>
      <td className="r mono">{num(row.eps_estimate)}</td>
      <td className={`r mono ${tone}`}>
        {num(row.eps_actual)}
        {row.eps_actual != null && row.surprise_pct != null ? <span className="calendar-surprise">{formatPercent(row.surprise_pct, locale)}</span> : null}
      </td>
    </tr>
  );
}
