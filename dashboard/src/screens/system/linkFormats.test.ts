import { describe, expect, it } from "vitest";
import { normalizeRepo, normalizeServerName, repoProblem, serverProblem } from "./linkFormats";

describe("normalizeRepo", () => {
  it.each([
    ["acme/plugins", "acme/plugins"],
    ["  acme/plugins  ", "acme/plugins"],
    ["https://github.com/acme/plugins", "acme/plugins"],
    ["https://github.com/acme/plugins/", "acme/plugins"],
    ["https://github.com/acme/plugins.git", "acme/plugins"],
    ["https://github.com/acme/plugins.git/", "acme/plugins"],
    ["http://www.github.com/acme/plugins", "acme/plugins"],
    ["github.com/acme/plugins", "acme/plugins"],
    ["https://github.com/acme/plugins/tree/main?x=1#y", "acme/plugins"],
  ])("%s -> %s", (input, expected) => {
    expect(normalizeRepo(input)).toBe(expected);
  });

  it.each(["", "acme", "acme/plugins/extra", "https://gitlab.com/acme/plugins", "git@github.com:acme/plugins.git", "https://github.com/acme", "acme/pl ugins"])(
    "rejects %j",
    (input) => {
      expect(normalizeRepo(input)).toBeNull();
    },
  );

  it("says why: empty or format", () => {
    expect(repoProblem("  ")).toBe("empty");
    expect(repoProblem("nope")).toBe("format");
    expect(repoProblem("https://github.com/a/b.git")).toBeNull();
  });
});

describe("normalizeServerName", () => {
  it("lowercases, drops accents and turns spaces into hyphens", () => {
    expect(normalizeServerName("Mi Servidor \u00d1and\u00fa")).toBe("mi-servidor-nandu");
    expect(normalizeServerName("Lauz")).toBe("lauz");
    expect(normalizeServerName("  cafe   con  leche ")).toBe("cafe-con-leche-");
    expect(normalizeServerName("C\u00f3digo_API")).toBe("codigo_api");
  });

  it("never exceeds 32 characters or starts with a separator", () => {
    expect(normalizeServerName("a".repeat(40))).toHaveLength(32);
    expect(normalizeServerName("-_x")).toBe("x");
  });
});

describe("serverProblem", () => {
  it("names the reason", () => {
    expect(serverProblem("", "https://x.test")).toBe("name_empty");
    expect(serverProblem("hokma", "https://x.test")).toBe("name_reserved");
    expect(serverProblem("lauz", "")).toBe("url_empty");
    expect(serverProblem("lauz", "http://x.test")).toBe("url_format");
    expect(serverProblem("lauz", "https://x.test/mcp")).toBeNull();
  });
});
