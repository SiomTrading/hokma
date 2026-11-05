import { describe, expect, it } from "vitest";

const sources = import.meta.glob<string>(["./**/*.{ts,tsx,css}", "!./**/*.test.{ts,tsx}"], { eager: true, query: "?raw", import: "default" });

describe("source rules", () => {
  it("scans the source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20);
  });

  it("never injects raw HTML", () => {
    const forbidden = ["dangerously", "SetInnerHTML"].join("");
    for (const [file, text] of Object.entries(sources)) expect(text.includes(forbidden), file).toBe(false);
  });

  it("carries no sample data from the design", () => {
    const samples = ["Camila", "Fondo Cedro", "HKM-7Q2D", "R\u00edos"];
    for (const [file, text] of Object.entries(sources)) {
      for (const sample of samples) expect(text.includes(sample), `${file}: ${sample}`).toBe(false);
    }
  });

  it("loads fonts only from local files", () => {
    for (const [file, text] of Object.entries(sources)) expect(text.includes("fonts.googleapis"), file).toBe(false);
  });
});
