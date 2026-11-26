import { api } from "@/api";

export type CalendarFilter = "all" | "list" | "liquid";

export interface CalendarRow {
  symbol: string;
  date: string;
  /** bmo before the open, amc after the close, dmh during the session; null when the source does not say. */
  hour: string | null;
  eps_estimate: number | null;
  eps_actual: number | null;
  surprise_pct: number | null;
  in_list: boolean;
}

export interface CalendarDay {
  date: string;
  /** True: the market is closed that weekday. Null: the broker connection could not say. */
  holiday: boolean | null;
  /** The provider cut the day: there are more reports than the rows shown. */
  truncated: boolean;
  /** The provider refused this day; the rows are then empty and it is not "no reports". */
  error: string | null;
  rows: CalendarRow[];
}

/** `GET /api/desks/{id}/earnings-calendar`. Without a data connection `connected` is false and there are no days. */
export interface CalendarWeek {
  from: string;
  to: string;
  filter: CalendarFilter;
  today: string;
  connected: boolean;
  source: string | null;
  holidays_known: boolean;
  /** Only with the liquid filter: tickers whose options were read, and tickers that could not be read (they are not shown). */
  liquid: { checked: number; unchecked: number } | null;
  days: CalendarDay[];
}

export const calendarApi = {
  week: (deskId: number, from: string, to: string, filter: CalendarFilter) =>
    api.get<CalendarWeek>(`/api/desks/${deskId}/earnings-calendar?from=${from}&to=${to}&filter=${filter}`),
};
