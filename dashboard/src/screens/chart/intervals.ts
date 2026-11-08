import { t as translate } from "@/i18n";
import type { Params } from "@/i18n";

/**
 * The chart interval grammar, the same one the engine applies: a time interval (`30s`, `7m`, `3h`, `2D`, `1W`) or a
 * range (`10r` ticks, `2.5rp` price, canonical `r:10t` / `r:2.5p`). The shared table of cases in
 * `backend/tests/market_fixtures/intervals_cases.json` is the authority for both sides.
 *
 * - seconds 1..3600, minutes 1..1440, hours 1..168, days (sessions) 1..365, weeks 1..52;
 * - a whole number of the next unit is promoted: `60s` is `1m`, `3600s` and `60m` are `1h`. Hours never become days
 *   and days never become weeks;
 * - `s`, `m`, `h` are lowercase; `d` and `w` are written as `D` and `W`; a capital `M` means months, not available;
 * - spaces around the text are trimmed, spaces inside are invalid; leading zeros are fine.
 */

export type TimeUnit = "s" | "m" | "h" | "D" | "W";
export type RangeUnit = "t" | "p";

export interface TimeInterval {
  kind: "time";
  n: number;
  unit: TimeUnit;
  /** Nominal length: `1D` gives 86400 even though a day counts sessions. */
  seconds: number;
  canonical: string;
}

export interface RangeInterval {
  kind: "range";
  size: number;
  unit: RangeUnit;
  canonical: string;
}

export type Interval = TimeInterval | RangeInterval;
export type IntervalError = "months" | "invalid";
export type IntervalParse = { ok: true; interval: Interval } | { ok: false; error: IntervalError };

type Translate = (key: string, params?: Params) => string;

export const TIME_UNITS: readonly TimeUnit[] = ["s", "m", "h", "D", "W"];
export const MAX_RANGE_TICKS = 10000;

const LIMITS: Record<TimeUnit, number> = { s: 3600, m: 1440, h: 168, D: 365, W: 52 };
const UNIT_SECONDS: Record<TimeUnit, number> = { s: 1, m: 60, h: 3600, D: 86400, W: 604800 };

const TIME = /^([0-9]+)([smhDdWw])$/;
const MONTHS = /^[0-9]*M$/;
const TICKS_SHORT = /^([0-9]+)r$/;
const TICKS_CANON = /^r:([0-9]+)t$/;
const PRICE_SHORT = /^([0-9]+(?:\.[0-9]+)?)rp$/;
const PRICE_CANON = /^r:([0-9]+(?:\.[0-9]+)?)p$/;

const fail = (error: IntervalError): IntervalParse => ({ ok: false, error });

function timeOf(n: number, written: string): IntervalParse {
  let unit = (written === "d" ? "D" : written === "w" ? "W" : written) as TimeUnit;
  if (!(n >= 1 && n <= LIMITS[unit])) return fail("invalid");
  if (unit === "s" && n % 60 === 0) {
    n /= 60;
    unit = "m";
  }
  if (unit === "m" && n % 60 === 0) {
    n /= 60;
    unit = "h";
  }
  return { ok: true, interval: { kind: "time", n, unit, seconds: n * UNIT_SECONDS[unit], canonical: `${n}${unit}` } };
}

function rangeOf(digits: string, unit: RangeUnit): IntervalParse {
  if (unit === "t") {
    if (digits.includes(".")) return fail("invalid");
    const size = Number.parseInt(digits, 10);
    if (!(size >= 1 && size <= MAX_RANGE_TICKS)) return fail("invalid");
    return { ok: true, interval: { kind: "range", size, unit: "t", canonical: `r:${size}t` } };
  }
  const size = Number.parseFloat(digits);
  if (!(size > 0) || !Number.isFinite(size)) return fail("invalid");
  return { ok: true, interval: { kind: "range", size, unit: "p", canonical: `r:${String(size)}p` } };
}

/** Parse and normalize what the owner wrote, or a canonical value read back from storage. */
export function parseInterval(text: string): IntervalParse {
  if (typeof text !== "string") return fail("invalid");
  const value = text.trim();
  if (MONTHS.test(value)) return fail("months");
  const time = TIME.exec(value);
  if (time) return timeOf(Number.parseInt(time[1]!, 10), time[2]!);
  const ranges: ReadonlyArray<[RegExp, RangeUnit]> = [
    [TICKS_SHORT, "t"],
    [TICKS_CANON, "t"],
    [PRICE_SHORT, "p"],
    [PRICE_CANON, "p"],
  ];
  for (const [pattern, unit] of ranges) {
    const found = pattern.exec(value);
    if (found) return rangeOf(found[1]!, unit);
  }
  return fail("invalid");
}

/** The canonical form (`7m`, `1h`, `r:10t`, `r:2.5p`), or null when the text is not an interval. */
export function canonicalInterval(text: string): string | null {
  const parsed = parseInterval(text);
  return parsed.ok ? parsed.interval.canonical : null;
}

export function isValidInterval(text: string): boolean {
  return parseInterval(text).ok;
}

/** True for time intervals shorter than a minute: they need a provider that gives ticks. */
export function isSeconds(value: string | Interval): boolean {
  const interval = typeof value === "string" ? (parseInterval(value) as { interval?: Interval }).interval : value;
  return !!interval && interval.kind === "time" && interval.seconds < 60;
}

export function isRange(value: string | Interval): boolean {
  const interval = typeof value === "string" ? (parseInterval(value) as { interval?: Interval }).interval : value;
  return !!interval && interval.kind === "range";
}

/** Catalog text of a grammar error. */
export function intervalErrorText(error: IntervalError, t: Translate = translate): string {
  return error === "months" ? t("chart.interval.months") : t("chart.interval.invalid");
}

/** What the title and the favorites show: `7m`, `Range 10t`, `Range 2.5`. Unknown text is shown as written. */
export function intervalLabel(value: string, t: Translate = translate): string {
  const parsed = parseInterval(value);
  if (!parsed.ok) return value;
  const interval = parsed.interval;
  if (interval.kind === "time") return interval.canonical;
  const size = interval.unit === "t" ? `${interval.size}t` : String(interval.size);
  return t("chart.interval.rangeTitle", { size });
}

/** The chart title: `NQ=F · 7m`, `NQ=F · Range 10t`, `NQ=F · Range 2.5`. */
export function chartTitle(symbol: string, interval: string, t: Translate = translate): string {
  return `${symbol} · ${intervalLabel(interval, t)}`;
}

export interface IntervalSuggestion {
  canonical: string;
  unit: TimeUnit;
  seconds: number;
}

/**
 * Suggestions while the owner types. A bare whole number gives one interval per unit that accepts it (`7` gives
 * `7s 7m 7h 7D 7W`; `90` gives `90s 90m 90h 90D`), normalized and without repeats. A number with a unit gives that
 * interval when it is valid. Anything else gives nothing.
 */
export function intervalSuggestions(text: string): IntervalSuggestion[] {
  const value = text.trim();
  const out: IntervalSuggestion[] = [];
  const seen = new Set<string>();
  const add = (candidate: string) => {
    const parsed = parseInterval(candidate);
    if (!parsed.ok || parsed.interval.kind !== "time" || seen.has(parsed.interval.canonical)) return;
    seen.add(parsed.interval.canonical);
    out.push({ canonical: parsed.interval.canonical, unit: parsed.interval.unit, seconds: parsed.interval.seconds });
  };
  if (/^[0-9]+$/.test(value)) {
    for (const unit of TIME_UNITS) add(`${value}${unit}`);
  } else if (TIME.test(value)) {
    add(value);
  }
  return out;
}

/** Sort order of favorites: time by length, then ranges in ticks, then ranges in price, each by size. */
export function compareIntervals(a: string, b: string): number {
  const pa = parseInterval(a);
  const pb = parseInterval(b);
  const rank = (p: IntervalParse): [number, number] => {
    if (!p.ok) return [3, 0];
    const i = p.interval;
    if (i.kind === "time") return [0, i.seconds];
    return [i.unit === "t" ? 1 : 2, i.size];
  };
  const [ga, va] = rank(pa);
  const [gb, vb] = rank(pb);
  return ga !== gb ? ga - gb : va - vb;
}
