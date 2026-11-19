import { api } from "@/api";
import { jobsApi } from "@/screens/agents/api";

export interface TodayAsset {
  symbol: string;
  last: number | null;
  change_pct?: number | null;
  /** Why there is no price (no connection, unknown symbol). */
  error?: string;
  desks: string[];
}
export interface TodayEvent {
  time: string;
  country: string;
  event: string;
  impact: "low" | "medium" | "high";
  actual: string | null;
  estimate: string | null;
  previous: string | null;
}
export interface TodayNews {
  time: string | null;
  headline: string;
  source: string;
}
/** A section the card could not fill says why in `error`; a section switched off comes as null. */
type Section<T> = ({ error?: string } & Partial<T>) | null;

export interface TodayData {
  config: { briefing_job_id: number | null; sections: Record<string, boolean> } & Record<string, unknown>;
  watchlist: Section<{ assets: TodayAsset[]; as_of: string }>;
  calendar: Section<{ events: TodayEvent[]; source: string }>;
  news: Section<{ news: TodayNews[] }>;
  briefing: { text: string; task_id: number; as_of: string } | null;
}

export const todayApi = {
  get: () => api.get<TodayData>("/api/today"),
  refresh: () => api.post<TodayData>("/api/today/refresh"),
  /**
   * "Generate now": makes sure the premarket routine exists, links it to the card and runs it when the owner already
   * approved it. Returns `"needs-approval"` when the owner has to switch the routine on first (Tasks does that).
   */
  generateBriefing: async (data: TodayData | null): Promise<"started" | "needs-approval"> => {
    const job = await jobsApi.addBriefing();
    if (data?.config.briefing_job_id !== job.id) {
      await api.put("/api/today/config", { config: { ...(data?.config ?? {}), briefing_job_id: job.id } });
    }
    if (!job.approved) return "needs-approval";
    await jobsApi.runNow(job.id);
    return "started";
  },
};
