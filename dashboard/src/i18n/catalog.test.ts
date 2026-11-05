import { describe, expect, it } from "vitest";
import { getCatalog, registerMessages, setLocale, t, defineMessages } from "@/i18n";
import type { MessageModule } from "@/i18n";

const modules = import.meta.glob<{ default: MessageModule }>(["./messages/*.ts"], { eager: true });

describe("message catalog", () => {
  it("every module has the same keys in es and en", () => {
    for (const [file, mod] of Object.entries(modules)) {
      const es = Object.keys(mod.default.es).sort();
      const en = Object.keys(mod.default.en).sort();
      expect(en, file).toEqual(es);
    }
  });

  it("no text is empty and es and en are loaded for the same keys", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    expect(Object.keys(en).sort()).toEqual(Object.keys(es).sort());
    for (const [key, text] of [...Object.entries(es), ...Object.entries(en)]) {
      expect(text.trim(), key).not.toBe("");
    }
  });

  it("placeholders match between languages", () => {
    const es = getCatalog("es");
    const en = getCatalog("en");
    const vars = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(",");
    for (const key of Object.keys(es)) expect(vars(en[key]!), key).toBe(vars(es[key]!));
  });

  it("translates, interpolates and falls back to the key", () => {
    expect(t("nav.settings")).toBe("Ajustes");
    setLocale("en");
    expect(t("nav.settings")).toBe("Settings");
    expect(t("top.closeTab", { title: "X" })).toBe("Close X");
    expect(t("does.not.exist")).toBe("does.not.exist");
  });

  it("rejects a key defined by two modules", () => {
    const a = defineMessages({ es: { "dup.test": "a" }, en: { "dup.test": "a" } });
    const b = defineMessages({ es: { "dup.test": "b" }, en: { "dup.test": "b" } });
    registerMessages(a);
    expect(() => registerMessages(b)).toThrow(/Duplicate/);
  });
});
