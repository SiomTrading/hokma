import type { JobItem, ModelAlias } from "./types";

type Translate = (key: string, params?: Record<string, string | number>) => string;

export const MODELS: readonly ModelAlias[] = ["haiku", "sonnet", "opus"];
export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Day = (typeof DAYS)[number];

/** Haiku, then Sonnet, then Opus, then back to Haiku (the pill of the design). */
export function nextModel(model: ModelAlias): ModelAlias {
  return MODELS[(MODELS.indexOf(model) + 1) % MODELS.length] ?? "sonnet";
}

/** An agent file's description may carry usage examples after the summary; the list shows only the summary. */
export function shortDescription(text: string): string {
  const cut = text.search(/<example|\bExamples?:/i);
  return (cut > 0 ? text.slice(0, cut) : text).replace(/\s+/g, " ").trim();
}

/** The database writes UTC as `YYYY-MM-DD HH:MM:SS`. */
export function parseDbTime(value: string): Date {
  return new Date(`${value.replace(" ", "T")}Z`);
}

export function formatDateTime(value: string | null, locale: string, timeZone?: string): string {
  if (!value) return "";
  const date = parseDbTime(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short", ...(timeZone ? { timeZone } : {}) });
}

export function formatDate(value: string, locale: string): string {
  const date = parseDbTime(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(locale, { dateStyle: "medium" });
}

export function formatTokens(value: number, locale: string): string {
  return value.toLocaleString(locale);
}

/** Days of a weekly expression ("mon,wed 07:30") as a list. */
export function parseWeekly(expr: string): { days: Day[]; time: string } {
  const index = expr.lastIndexOf(" ");
  const days = expr.slice(0, index).split(",").filter((d): d is Day => (DAYS as readonly string[]).includes(d));
  return { days, time: expr.slice(index + 1) };
}

function daysText(days: Day[], t: Translate): string {
  const key = days.join(",");
  if (key === "mon,tue,wed,thu,fri") return `${t("tasks.day.mon")} - ${t("tasks.day.fri")}`;
  return days.map((d) => t(`tasks.day.${d}`)).join(", ");
}

function intervalText(expr: string, t: Translate): string {
  const unit = expr.slice(-1);
  return t(`tasks.when.every.${unit}`, { n: expr.slice(0, -1) });
}

/** "Every day, 07:30" in the user's language. `once` shows the local date and time of the job's zone. */
export function describeSchedule(job: Pick<JobItem, "kind" | "expr" | "timezone">, t: Translate, locale: string): string {
  switch (job.kind) {
    case "once": {
      const date = new Date(`${job.expr}:00Z`);
      const text = Number.isNaN(date.getTime())
        ? job.expr
        : date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" });
      return t("tasks.when.once", { date: text });
    }
    case "daily":
      return t("tasks.when.daily", { time: job.expr });
    case "weekly": {
      const { days, time } = parseWeekly(job.expr);
      return t("tasks.when.weekly", { days: daysText(days, t), time });
    }
    default:
      return t("tasks.when.interval", { every: intervalText(job.expr, t) });
  }
}

/** The browser's time zone, used as the default of a new task. */
export function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function timeZones(): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
  try {
    return supported ? supported("timeZone") : [];
  } catch {
    return [];
  }
}

const AGENT_TINTS = 5;

/** Stable per-agent colour token: the same slug always maps to the same one, in dark and light. */
export function agentTint(slug: string): string {
  let hash = 0;
  for (let i = 0; i < slug.length; i += 1) hash = (hash * 31 + slug.charCodeAt(i)) >>> 0;
  return "var(--ag-" + String((hash % AGENT_TINTS) + 1) + ")";
}
