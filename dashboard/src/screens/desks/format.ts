/** Capital, whole units, grouped by the language ("10.000" in Spanish, "10,000" in English). */
export function formatCapital(value: number, locale: string): string {
  return value.toLocaleString(locale, { maximumFractionDigits: 0 });
}

/** Result of the day with two decimals and its sign ("+120,00", "-35,50", "0,00"). */
export function formatResult(value: number, locale: string): string {
  const text = Math.abs(value).toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (value > 0) return `+${text}`;
  if (value < 0) return `-${text}`;
  return text;
}

/** A percentage with one decimal ("2,1%"). */
export function formatPercent(value: number, locale: string): string {
  return `${value.toLocaleString(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

/** Token counts with the language's grouping ("300.000"). */
export function formatTokens(value: number, locale: string): string {
  return value.toLocaleString(locale, { maximumFractionDigits: 0 });
}

export type ResultTone = "up" | "down" | "flat";

export function resultTone(value: number): ResultTone {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/** Equity of an account, or "not available" when the broker cannot tell: never a made-up 0. */
export function formatBalance(value: number | null, locale: string, unavailable: string, currency?: string): string {
  if (value === null) return unavailable;
  const text = value.toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${text} ${currency}` : text;
}
