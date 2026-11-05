import { readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { getCatalog } from "@/i18n";

const KIT_AGENTS = resolve(__dirname, "../../../kit/template/agents");

function kitSlugs(): string[] {
  return readdirSync(KIT_AGENTS)
    .filter((file) => file.endsWith(".md"))
    .map((file) => file.slice(0, -3))
    .sort();
}

describe("kit agent descriptions", () => {
  it("the kit ships agents", () => {
    expect(kitSlugs().length).toBeGreaterThan(0);
  });

  it("every agent of the kit has its description in es and en", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    for (const slug of kitSlugs()) {
      const key = `agents.kit.${slug}.desc`;
      expect(es[key], `es ${key}`).toBeTruthy();
      expect(en[key], `en ${key}`).toBeTruthy();
    }
  });

  it("the catalog has no description for an agent the kit does not ship", () => {
    const slugs = new Set(kitSlugs());
    for (const key of Object.keys(getCatalog("es"))) {
      const match = /^agents\.kit\.(.+)\.desc$/.exec(key);
      if (match) expect(slugs.has(match[1]!), key).toBe(true);
    }
  });
});
