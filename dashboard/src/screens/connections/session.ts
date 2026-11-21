import type { SchwabSession } from "./types";

const DAY_MS = 86_400_000;

/** `session_expires_at` as a date: epoch seconds, epoch milliseconds or an ISO text. */
export function expiresAt(session: SchwabSession | null | undefined): Date | null {
  const raw = session?.session_expires_at;
  if (raw === null || raw === undefined || raw === "") return null;
  const date = typeof raw === "number" ? new Date(raw < 1e12 ? raw * 1000 : raw) : new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Whole days left, rounded up ("7 d" right after signing in). Zero or less when it ends within the day or already ended. */
export function daysLeft(session: SchwabSession | null | undefined, now: number = Date.now()): number {
  const date = expiresAt(session);
  return date ? Math.ceil((date.getTime() - now) / DAY_MS) : 0;
}

/** "tomorrow, 9:41 AM" in the active language: the day by name when it is today or tomorrow, else a short date. */
export function whenText(date: Date, locale: string, now: number = Date.now()): string {
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const gap = Math.round((midnight(date) - midnight(new Date(now))) / DAY_MS);
  const day =
    Math.abs(gap) <= 1
      ? new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(gap, "day")
      : new Intl.DateTimeFormat(locale, { weekday: "short", day: "numeric", month: "short" }).format(date);
  const time = new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit" }).format(date);
  return day + ", " + time;
}
