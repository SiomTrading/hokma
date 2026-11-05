import type { Candle, Timeframe } from "./types";

function safeZone(timeZone: string): string | undefined {
  try {
    new Intl.DateTimeFormat("en", { timeZone });
    return timeZone;
  } catch {
    return undefined;
  }
}

export function formatPrice(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const digits = Math.abs(value) >= 1 ? 2 : Math.abs(value) >= 0.01 ? 4 : 6;
  return new Intl.NumberFormat(locale, { minimumFractionDigits: digits === 2 ? 2 : 0, maximumFractionDigits: digits }).format(value);
}

export function formatPercent(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const text = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(value));
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}${text}%`;
}

export function formatVolume(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** Label for a bar: time for intraday bars that span less than a day, date and time for longer spans, date for daily. */
export function formatBarTime(epoch: number, timeframe: Timeframe, timeZone: string, locale: string, spanSeconds: number): string {
  const tz = safeZone(timeZone);
  const date = new Date(epoch * 1000);
  if (timeframe === "1D") return new Intl.DateTimeFormat(locale, { timeZone: tz, day: "numeric", month: "short", year: "2-digit" }).format(date);
  if (spanSeconds > 20 * 3600) {
    return new Intl.DateTimeFormat(locale, { timeZone: tz, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  return new Intl.DateTimeFormat(locale, { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

export function candleSpan(candles: Candle[]): number {
  return candles.length > 1 ? candles[candles.length - 1]!.t - candles[0]!.t : 0;
}

/** Date and time with the time zone of the exchange, for "last data" and "data as of". */
export function formatMoment(epoch: number, timeZone: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone: safeZone(timeZone),
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(epoch * 1000));
}

export function formatSessionDate(isoDate: string, locale: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  if (!y || !m || !d) return isoDate;
  return new Intl.DateTimeFormat(locale, { timeZone: "UTC", day: "numeric", month: "short" }).format(new Date(Date.UTC(y, m - 1, d)));
}

export function safeFileName(symbol: string, timeframe: string): string {
  return `chart-${symbol.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "")}-${timeframe}.png`;
}
