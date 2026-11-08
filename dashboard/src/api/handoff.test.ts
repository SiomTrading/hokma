import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { provideHandoff, reloadWithHandoff, resetHandoffForTests, takeHandoff } from "./handoff";

const PREFIX = "hokma-handoff:";
const here = () => window.location.pathname + window.location.search;
const payload = (extra: Record<string, unknown>) =>
  PREFIX + JSON.stringify({ o: window.location.origin, u: here(), ...extra });

describe("handoff through window.name", () => {
  beforeEach(() => {
    window.name = "";
    resetHandoffForTests();
    provideHandoff("t", () => "tokentokentoken");
    provideHandoff("n", () => "0123456789abcdef0123456789abcdef");
  });

  afterEach(() => {
    vi.useRealTimers();
    window.name = "";
  });

  it("writes nothing when the page leaves: pagehide, beforeunload and the reload keys", () => {
    window.dispatchEvent(new Event("pagehide"));
    window.dispatchEvent(new Event("beforeunload"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "F5" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "r", ctrlKey: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "R", metaKey: true }));
    expect(window.name).toBe("");
  });

  it("writes only inside reloadWithHandoff, with origin and address, and then reloads", () => {
    const reload = vi.fn(() => {
      expect(window.name.startsWith(PREFIX)).toBe(true);        // already written when the reload starts
    });
    reloadWithHandoff(reload);
    expect(reload).toHaveBeenCalledTimes(1);
    const body = JSON.parse(window.name.slice(PREFIX.length));
    expect(body).toEqual({ o: window.location.origin, u: here(), t: "tokentokentoken", n: "0123456789abcdef0123456789abcdef" });
  });

  it("takes the copy back when the page does not leave", () => {
    vi.useFakeTimers();
    reloadWithHandoff(() => undefined);
    expect(window.name).not.toBe("");
    vi.advanceTimersByTime(3500);
    expect(window.name).toBe("");
  });

  it("hands the values to the next load of the same origin and address, once, and empties the name", () => {
    reloadWithHandoff(() => undefined);
    resetHandoffForTests();
    expect(takeHandoff("t")).toBe("tokentokentoken");
    expect(takeHandoff("t")).toBeNull();
    expect(takeHandoff("n")).toBe("0123456789abcdef0123456789abcdef");
    expect(window.name).toBe("");
  });

  it("drops it, and empties the name, when the origin is another", () => {
    window.name = PREFIX + JSON.stringify({ o: "https://other.example", u: here(), t: "tokentokentoken", n: "0123456789abcdef0123456789abcdef" });
    expect(takeHandoff("t")).toBeNull();
    expect(takeHandoff("n")).toBeNull();
    expect(window.name).toBe("");
  });

  it("drops it when the address is another", () => {
    window.name = PREFIX + JSON.stringify({ o: window.location.origin, u: "/somewhere/else", t: "tokentokentoken" });
    expect(takeHandoff("t")).toBeNull();
    expect(window.name).toBe("");
  });

  it("drops a payload without origin or address (the old form)", () => {
    window.name = PREFIX + JSON.stringify({ t: "tokentokentoken", n: "0123456789abcdef0123456789abcdef" });
    expect(takeHandoff("t")).toBeNull();
    expect(window.name).toBe("");
  });

  it.each(["not json", "null", "[1,2]", '"text"', "{", '{"o":5,"u":6,"t":7}'])("drops a malformed payload: %s", (raw) => {
    window.name = PREFIX + raw;
    expect(takeHandoff("t")).toBeNull();
    expect(takeHandoff("n")).toBeNull();
    expect(window.name).toBe("");
  });

  it("ignores values that are not text", () => {
    window.name = payload({ t: 5, n: { a: 1 } });
    expect(takeHandoff("t")).toBeNull();
    expect(takeHandoff("n")).toBeNull();
  });

  it("never puts the owner nonce in a handoff for any other navigation", () => {
    for (const event of ["pagehide", "beforeunload", "unload", "visibilitychange", "popstate", "hashchange"]) {
      window.dispatchEvent(new Event(event));
    }
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "F5" }));
    expect(window.name).not.toContain("0123456789abcdef0123456789abcdef");
    expect(window.name).toBe("");
  });

  it("leaves a window.name that is not ours alone", () => {
    window.name = "someone-else";
    expect(takeHandoff("t")).toBeNull();
    expect(window.name).toBe("someone-else");
  });
});
