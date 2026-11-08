import { describe, expect, it } from "vitest";
import { setLocale, t } from "@/i18n";
import shared from "../../../../backend/tests/market_fixtures/intervals_cases.json";
import {
  canonicalInterval,
  chartTitle,
  compareIntervals,
  intervalErrorText,
  intervalLabel,
  intervalSuggestions,
  isRange,
  isSeconds,
  parseInterval,
} from "./intervals";

interface Case {
  input: string;
  ok: boolean;
  kind?: "time" | "range";
  n?: number;
  unit?: string;
  seconds?: number;
  size?: number;
  canonical?: string;
  error?: "months" | "invalid";
}

const cases = (shared as { cases: Case[] }).cases;

describe("interval grammar: the table shared with the engine (T-4)", () => {
  it("has at least 30 cases, with valid, normalized, invalid, months and range entries", () => {
    expect(cases.length).toBeGreaterThanOrEqual(30);
    const inputs = cases.map((c) => c.input);
    for (const needed of ["M", "r:10t", "2.5rp", "60s", "1440m", "7x"]) expect(inputs).toContain(needed);
  });

  it.each(cases.map((c) => [JSON.stringify(c.input), c] as const))("%s", (_label, c) => {
    const result = parseInterval(c.input);
    if (!c.ok) {
      expect(result).toEqual({ ok: false, error: c.error });
      return;
    }
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const got = result.interval;
    if (c.kind === "time") {
      expect(got).toEqual({ kind: "time", n: c.n, unit: c.unit, seconds: c.seconds, canonical: c.canonical });
    } else {
      expect(got).toEqual({ kind: "range", size: c.size, unit: c.unit, canonical: c.canonical });
    }
    // The canonical form reads back as itself: it is what storage and the protocol carry.
    expect(canonicalInterval(got.canonical)).toBe(got.canonical);
  });
});

describe("interval helpers", () => {
  it("suggests one interval per unit for a bare number, normalized and without repeats", () => {
    expect(intervalSuggestions("7").map((s) => s.canonical)).toEqual(["7s", "7m", "7h", "7D", "7W"]);
    expect(intervalSuggestions("60").map((s) => s.canonical)).toEqual(["1m", "1h", "60h", "60D"]);
    expect(intervalSuggestions("400").map((s) => s.canonical)).toEqual(["400s", "400m"]);
    expect(intervalSuggestions("7m").map((s) => s.canonical)).toEqual(["7m"]);
    expect(intervalSuggestions("")).toEqual([]);
    expect(intervalSuggestions("abc")).toEqual([]);
    expect(intervalSuggestions("10r")).toEqual([]);
  });

  it("tells seconds and ranges apart", () => {
    expect(isSeconds("15s")).toBe(true);
    expect(isSeconds("60s")).toBe(false); // that is 1m
    expect(isSeconds("7m")).toBe(false);
    expect(isSeconds("r:10t")).toBe(false);
    expect(isSeconds("nonsense")).toBe(false);
    expect(isRange("r:2.5p")).toBe(true);
    expect(isRange("10r")).toBe(true);
    expect(isRange("1D")).toBe(false);
  });

  it("builds the chart title for time and range intervals", () => {
    setLocale("en");
    expect(chartTitle("NQ=F", "7m")).toBe("NQ=F · 7m");
    expect(chartTitle("NQ=F", "r:10t")).toBe("NQ=F · Range 10t");
    expect(chartTitle("NQ=F", "r:2.5p")).toBe("NQ=F · Range 2.5");
    expect(intervalLabel("60m")).toBe("1h");
    setLocale("es");
    expect(chartTitle("NQ=F", "r:10t")).toBe("NQ=F · Rango 10t");
  });

  it("gives the catalog text of each grammar error", () => {
    setLocale("en");
    expect(intervalErrorText("months", t)).toBe("Months are not available yet");
    expect(intervalErrorText("invalid", t)).toBe("Write a number and a unit: 30s, 7m, 3h, 2D, 1W");
  });

  it("sorts time by length, then ranges in ticks, then ranges in price", () => {
    expect(["r:2.5p", "1D", "r:10t", "15s", "1h", "r:4t", "5m"].sort(compareIntervals)).toEqual(["15s", "5m", "1h", "1D", "r:4t", "r:10t", "r:2.5p"]);
  });
});
