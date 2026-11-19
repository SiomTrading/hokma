import type { TodayData } from "./todayApi";

/** What `GET /api/today` answers on a new install: every block on, nothing to show, no briefing. */
export const TODAY_EMPTY: TodayData = {
  config: { briefing_job_id: null, sections: { watchlist: true, calendar: true, news: true, briefing: true } },
  watchlist: { assets: [], as_of: "2026-11-19T12:00:00Z" },
  calendar: { events: [], source: "Forex Factory" },
  news: { news: [] },
  briefing: null,
};
