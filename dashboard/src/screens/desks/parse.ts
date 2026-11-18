/** Whole number from what the person typed ("10.000", "10,000" and "10000" all read as 10000). Null when empty. */
export function parseWhole(text: string): number | null {
  const digits = text.replace(/\D/g, "");
  return digits === "" ? null : Number(digits);
}

/** Decimal from what the person typed, with a comma or a dot. Null when empty or not a number. */
export function parseDecimal(text: string): number | null {
  const clean = text.trim().replace(",", ".");
  if (clean === "") return null;
  const value = Number(clean);
  return Number.isFinite(value) ? value : null;
}
