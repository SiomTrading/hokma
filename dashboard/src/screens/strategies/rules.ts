import type { BankPassage } from "@/api/strategyBank";

export interface RuleLine {
  path: string;
  value: string;
  origin: string | null;
  cite: number[];
}

const SKIP = new Set(["map", "schema", "needs_new_rule", "summary", "line"]);

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value);
}

/** The rules of a sheet of the bank, one line each: where it sits in the sheet, its value, where it comes from and the passages that back it. */
export function ruleLines(extraction: Record<string, unknown>): RuleLine[] {
  const out: RuleLine[] = [];
  const walk = (node: unknown, path: string[]) => {
    if (node === null || typeof node !== "object" || Array.isArray(node)) return;
    const item = node as Record<string, unknown>;
    if ("value" in item && ("origin" in item || "cite" in item)) {
      const cite = Array.isArray(item.cite) ? item.cite.filter((c): c is number => typeof c === "number") : [];
      out.push({ path: path.join(" / "), value: text(item.value), origin: typeof item.origin === "string" ? item.origin : null, cite });
      return;
    }
    for (const [key, child] of Object.entries(item)) if (!SKIP.has(key)) walk(child, [...path, key]);
  };
  walk(extraction, []);
  return out;
}

/** The locators of the cited passages ("min 4:10", "p. 12"), in the order of the citation. */
export function locators(cite: number[], passages: BankPassage[]): string[] {
  return cite.map((id) => passages.find((p) => p.passage_id === id)?.locator ?? `#${id}`);
}
