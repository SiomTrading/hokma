import { describe, expect, it } from "vitest";

/** C5, in the source: the keys and the providers are handled in the Connections screen and nowhere else. */
const sources = import.meta.glob<string>(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}", "!./test/**"], { eager: true, query: "?raw", import: "default" });

const filesWith = (needle: string | RegExp) =>
  Object.entries(sources)
    .filter(([, text]) => (typeof needle === "string" ? text.includes(needle) : needle.test(text)))
    .map(([file]) => file)
    .sort();

describe("keys and providers live in one place", () => {
  it("scans the source tree", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("only the Connections screen talks to /api/connections", () => {
    const files = filesWith("/api/connections");
    expect(files).toContain("./screens/connections/api.ts");
    for (const file of files) expect(file.startsWith("./screens/connections/"), file).toBe(true);
  });

  it("only the Connections card draws a password field or names a key field", () => {
    // The key of a data provider lives in the Connections card. The key of an AI engine (task 3127) is another feature,
    // in Settings > Engines and in the engines step of the first run, so its card is the only other password field.
    expect(filesWith('type="password"')).toEqual(["./screens/connections/ProviderCard.tsx", "./screens/engines/ConnectCards.tsx"]);
    for (const file of filesWith(/\bkey_id\b|\bsecret\b/)) {
      expect(file.startsWith("./screens/connections/") || file === "./i18n/messages/connections.ts", file).toBe(true);
    }
  });

  it("no other screen names a data provider", () => {
    const providers = /\b(alpaca|finnhub|coinbase|databento|massive)\b/i;
    for (const file of filesWith(providers)) {
      expect(file.startsWith("./screens/connections/") || file === "./i18n/messages/connections.ts", file).toBe(true);
    }
  });
});
