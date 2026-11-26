import { describe, expect, it } from "vitest";
import { getCatalog } from "@/i18n";
import module from "./messages/verdict";

const verdict: Record<"es" | "en", Record<string, string>> = module;

const FIELDS = ["label", "what", "why", "raise", "lower", "example"] as const;
const MEASURES = Array.from({ length: 13 }, (_, i) => `M${i + 1}`);
const CHECKS = [
  "point_in_time", "survivorship_free", "corporate_actions", "no_leakage", "data_gaps", "trials_counted", "in_sample_tuning", "holdout_untouched",
  "history_length", "costs_charged", "impact_modeled", "financing_and_borrow", "house_fill", "capacity", "validation_scheme", "multiple_testing",
  "chance_hurdle", "deflated_sharpe", "pbo", "parameter_neighbors", "regimes", "red_flags", "gross_and_net", "drawdown_analysis",
];
const QUESTIONS = [
  "point_in_time", "survivorship_free", "corporate_actions", "gaps_checked", "in_sample_tuning", "holdout_untouched", "impact_modeled",
  "borrow_costs_modeled", "capacity_checked",
];

describe("verdict texts (SPEC-032)", () => {
  it("the 13 measures have the six fields in Spanish and English", () => {
    for (const locale of ["es", "en"] as const) {
      for (const id of MEASURES) {
        for (const field of FIELDS) {
          const text = verdict[locale][`verdict.measure.${id}.${field}`];
          expect(text?.trim().length ?? 0, `${locale} ${id} ${field}`).toBeGreaterThan(5);
        }
      }
    }
  });

  it("M1 and M2 share the explanation and M11 and M12 too, with their own label", () => {
    for (const locale of ["es", "en"] as const) {
      for (const [a, b] of [["M1", "M2"], ["M11", "M12"]]) {
        for (const field of ["what", "why", "raise", "lower", "example"]) {
          expect(verdict[locale][`verdict.measure.${a}.${field}`]).toBe(verdict[locale][`verdict.measure.${b}.${field}`]);
        }
        expect(verdict[locale][`verdict.measure.${a}.label`]).not.toBe(verdict[locale][`verdict.measure.${b}.label`]);
      }
    }
  });

  it("the 24 checks and the 9 questions have a text in both languages", () => {
    for (const locale of ["es", "en"] as const) {
      for (const key of CHECKS) expect(verdict[locale][`verdict.check.${key}`], `${locale} ${key}`).toBeTruthy();
      for (const key of QUESTIONS) expect(verdict[locale][`verdict.question.${key}`], `${locale} ${key}`).toBeTruthy();
    }
    expect(Object.keys(verdict.es).filter((k) => k.startsWith("verdict.check.")).length).toBe(24);
    expect(Object.keys(verdict.es).filter((k) => k.startsWith("verdict.question.")).length).toBe(9);
  });

  it("is in the catalog and has no long dash", () => {
    const es = getCatalog("es");
    expect(es["verdict.measure.M3.what"]).toBeTruthy();
    for (const text of [...Object.values(verdict.es), ...Object.values(verdict.en)]) expect(text.includes("\u2014")).toBe(false);
  });
});
