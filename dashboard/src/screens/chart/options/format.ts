import type { Params } from "@/i18n";

/** Formatters of the option chain. The number format is the one of the language; a missing value is the `options.missing` text. */

type Tr = (key: string, params?: Params) => string;

const cache = new Map<string, Intl.NumberFormat>();
function numberFormat(locale: string, digits: number, percent = false, grouping = true): Intl.NumberFormat {
  const key = `${locale}|${digits}|${percent}|${grouping}`;
  let format = cache.get(key);
  if (!format) {
    format = new Intl.NumberFormat(locale, {
      style: percent ? "percent" : "decimal",
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      useGrouping: grouping,
    });
    cache.set(key, format);
  }
  return format;
}

const finite = (value: number | null | undefined): value is number => typeof value === "number" && Number.isFinite(value);

export function decimal(value: number | null | undefined, digits: number, locale: string, t: Tr): string {
  return finite(value) ? numberFormat(locale, digits).format(value) : t("options.missing");
}

export function whole(value: number | null | undefined, locale: string, t: Tr): string {
  return finite(value) ? numberFormat(locale, 0).format(Math.round(value)) : t("options.missing");
}

/** A fraction (0.8238) as a percent with 2 decimals: `82.38%` in English, `82,38 %` in Spanish. */
export function percent(value: number | null | undefined, locale: string, t: Tr, digits = 2): string {
  return finite(value) ? numberFormat(locale, digits, true).format(value) : t("options.missing");
}

/** A price with its exchange letter after a space, only when the feed brings the letter. */
export function priced(value: number | null | undefined, exchange: string | null | undefined, locale: string, t: Tr): string {
  if (!finite(value)) return t("options.missing");
  const text = decimal(value, 2, locale, t);
  return exchange ? `${text} ${exchange}` : text;
}

/** `18 DEC 26` in English, `18 DIC 26` in Spanish: day, month in capitals without a dot, year of two digits. */
export function groupDate(iso: string, locale: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const parts = new Intl.DateTimeFormat(locale, { timeZone: "UTC", day: "2-digit", month: "short", year: "2-digit" }).formatToParts(
    new Date(Date.UTC(y, m - 1, d)),
  );
  const pick = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${pick("day")} ${pick("month").replace(/\./g, "").toUpperCase()} ${pick("year")}`;
}

/** The moment of the last quote in New York, for the market closed line. */
export function quoteMoment(iso: string | null | undefined, locale: string): string {
  const time = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(time)) return "";
  return new Intl.DateTimeFormat(locale, {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(time));
}
