import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// jsdom does not load the stylesheet, so the rule is checked in the source: .hint and .err must style any element
// that carries them, not only the ones inside a form field (1166).
const css = readFileSync("src/design/design.css", "utf8");

describe("hint and error text", () => {
  it("has a global small style for .hint and .err", () => {
    expect(css).toMatch(/^\.hint \{[^}]*font: 400 12px/m);
    expect(css).toMatch(/^\.err \{[^}]*font: 500 12px/m);
  });
});
