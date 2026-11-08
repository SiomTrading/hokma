import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MAX_TABS,
  TABS_KEY,
  VIEW_DELAY_MS,
  activateTab,
  cancelPending,
  defaultInterval,
  dropClosed,
  flushPending,
  moveTab,
  parseView,
  queueView,
  readLast,
  readTab,
  saveTabInterval,
  seedTab,
  tabKeys,
} from "./chartState";
import type { ChartViewState } from "./chartState";

const VIEW: ChartViewState = { follow: false, barSpacing: 6.4, rightOffset: 4, from: 1762340400, to: 1762369800, price: { auto: true } };
const stored = () => JSON.parse(localStorage.getItem(TABS_KEY) ?? "null");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-05T15:00:00Z"));
});
afterEach(() => {
  flushPending();
  vi.useRealTimers();
});

describe("reading", () => {
  it("gives the defaults when nothing is saved", () => {
    expect(readTab("chart:symbol=NQ=F")).toBeNull();
    expect(readLast()).toBeNull();
    expect(defaultInterval()).toBe("5m");
  });

  it("broken JSON, an unknown version or the wrong shape read as nothing and never throw", () => {
    for (const raw of ["{not json", "[]", "null", '{"v":2,"tabs":{"a":{"tf":"5m"}}}', '{"v":1,"tabs":7}', "123"]) {
      localStorage.setItem(TABS_KEY, raw);
      expect(readTab("a")).toBeNull();
      expect(readLast()).toBeNull();
    }
  });

  it("a rejected interval becomes the default one and the view with it; the other tabs stay", () => {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify({
        v: 1,
        tabs: { a: { tf: "99M", view: VIEW, updated_at: 1 }, b: { tf: "7m", view: VIEW, updated_at: 2 } },
        last: null,
      }),
    );
    expect(readTab("a")).toMatchObject({ tf: "5m", view: null });
    expect(readTab("b")).toMatchObject({ tf: "7m", view: VIEW });
  });

  it("uses the old global interval as the default when there is one", () => {
    localStorage.setItem("hokma.chart.timeframe", "15m");
    expect(defaultInterval()).toBe("15m");
  });

  it("ignores fields it does not know and keeps a list in indicators", () => {
    localStorage.setItem(TABS_KEY, JSON.stringify({ v: 1, tabs: { a: { tf: "5m", view: null, indicators: [{ id: "x" }], later: 1 } }, last: null, more: true }));
    expect(readTab("a")).toMatchObject({ tf: "5m", indicators: [{ id: "x" }] });
  });

  it.each([
    ["no follow", { ...VIEW, follow: undefined }],
    ["zero spacing", { ...VIEW, barSpacing: 0 }],
    ["from after to", { ...VIEW, from: 10, to: 5 }],
    ["a price range upside down", { ...VIEW, price: { auto: false, min: 5, max: 1 } }],
    ["a string number", { ...VIEW, rightOffset: "4" }],
    ["NaN", { ...VIEW, from: Number.NaN }],
  ])("a view with %s is no view", (_name, view) => {
    expect(parseView(view)).toBeNull();
  });

  it("accepts a manual price range", () => {
    const view = { ...VIEW, price: { auto: false, min: 100, max: 200 } };
    expect(parseView(view)).toEqual(view);
  });
});

describe("writing", () => {
  it("activating a tab creates its entry and makes it the last chart", () => {
    activateTab("chart:symbol=NQ=F", "NQ=F", "7m", null);
    expect(readTab("chart:symbol=NQ=F")).toMatchObject({ tf: "7m", view: null, indicators: [] });
    expect(readLast()).toMatchObject({ symbol: "NQ=F", tf: "7m", view: null });
    expect(stored().v).toBe(1);
  });

  it("activating a tab that has an entry keeps its interval and view", () => {
    seedTab("chart:symbol=ES=F", "15m", VIEW);
    activateTab("chart:symbol=ES=F", "ES=F", "5m", null);
    expect(readTab("chart:symbol=ES=F")).toMatchObject({ tf: "15m", view: VIEW });
    expect(readLast()).toMatchObject({ symbol: "ES=F", tf: "15m", view: VIEW });
  });

  it("a new interval resets the view and, when the tab is in front, becomes the last chart's", () => {
    seedTab("k", "5m", VIEW);
    saveTabInterval("k", "NQ=F", "3h", true);
    expect(readTab("k")).toMatchObject({ tf: "3h", view: null });
    expect(readLast()).toMatchObject({ symbol: "NQ=F", tf: "3h" });
  });

  it("a tab in the background does not take the last chart", () => {
    activateTab("a", "NQ=F", "5m", null);
    saveTabInterval("b", "ES=F", "1h", false);
    expect(readLast()).toMatchObject({ symbol: "NQ=F" });
    expect(readTab("b")).toMatchObject({ tf: "1h" });
  });

  it("an interval of the grammar is stored canonical; one that is not is ignored", () => {
    saveTabInterval("k", "NQ=F", "120m", true);
    expect(readTab("k")?.tf).toBe("2h");
    saveTabInterval("k", "NQ=F", "banana", true);
    expect(readTab("k")?.tf).toBe("2h");
  });

  it("keeps working when storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    expect(() => activateTab("k", "NQ=F", "5m", null)).not.toThrow();
    vi.restoreAllMocks();
  });
});

describe("the view waits 300 ms and goes out on pagehide", () => {
  it("writes once, 300 ms after the last movement", () => {
    activateTab("k", "NQ=F", "5m", null);
    queueView("k", "NQ=F", "5m", { ...VIEW, barSpacing: 5 }, true);
    vi.advanceTimersByTime(VIEW_DELAY_MS - 1);
    queueView("k", "NQ=F", "5m", { ...VIEW, barSpacing: 7 }, true);
    vi.advanceTimersByTime(VIEW_DELAY_MS - 1);
    expect(readTab("k")?.view).toBeNull(); // still waiting
    vi.advanceTimersByTime(1);
    expect(readTab("k")?.view?.barSpacing).toBe(7);
    expect(readLast()?.view?.barSpacing).toBe(7);
  });

  it("pagehide writes what is waiting (a reload right after a zoom)", () => {
    activateTab("k", "NQ=F", "5m", null);
    queueView("k", "NQ=F", "5m", VIEW, true);
    window.dispatchEvent(new Event("pagehide"));
    expect(readTab("k")?.view).toEqual(VIEW);
    vi.advanceTimersByTime(1000);
    expect(readTab("k")?.view).toEqual(VIEW); // and the timer does not write it twice
  });

  it("visibilitychange to hidden writes it too, visible does not", () => {
    activateTab("k", "NQ=F", "5m", null);
    queueView("k", "NQ=F", "5m", VIEW, true);
    const state = vi.spyOn(document, "visibilityState", "get");
    state.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(readTab("k")?.view).toBeNull();
    state.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(readTab("k")?.view).toEqual(VIEW);
  });

  it("a cancelled view is never written", () => {
    activateTab("k", "NQ=F", "5m", null);
    queueView("k", "NQ=F", "5m", VIEW, true);
    cancelPending("k");
    vi.advanceTimersByTime(1000);
    flushPending();
    expect(readTab("k")?.view).toBeNull();
  });
});

describe("a tab that changes asset", () => {
  it("moves the entry to the new key, keeps the interval, drops the view and leaves no old key", () => {
    seedTab("chart:symbol=NQ=F", "7m", VIEW);
    moveTab("chart:symbol=NQ=F", "chart:symbol=ES=F", false);
    expect(readTab("chart:symbol=NQ=F")).toBeNull();
    expect(readTab("chart:symbol=ES=F")).toMatchObject({ tf: "7m", view: null });
    expect(tabKeys()).toEqual(["chart:symbol=ES=F"]);
  });

  it("forgets a view that was waiting for the old key", () => {
    seedTab("old", "7m", null);
    queueView("old", "NQ=F", "7m", VIEW, true);
    moveTab("old", "new", false);
    vi.advanceTimersByTime(1000);
    expect(tabKeys()).toEqual(["new"]);
  });

  it("when the tabs merge the other one keeps its own state and this one goes", () => {
    seedTab("chart:symbol=NQ=F", "7m", VIEW);
    seedTab("chart:symbol=ES=F", "1h", { ...VIEW, barSpacing: 9 });
    moveTab("chart:symbol=NQ=F", "chart:symbol=ES=F", true);
    expect(tabKeys()).toEqual(["chart:symbol=ES=F"]);
    expect(readTab("chart:symbol=ES=F")).toMatchObject({ tf: "1h", view: { barSpacing: 9 } });
  });

  it("with nothing saved for the old key it does nothing", () => {
    moveTab("a", "b", false);
    expect(tabKeys()).toEqual([]);
  });
});

describe("cleanup", () => {
  it("on load only the open tabs keep an entry; last stays", () => {
    activateTab("open", "NQ=F", "5m", null);
    seedTab("closed", "1h", null);
    dropClosed(["open"]);
    expect(tabKeys()).toEqual(["open"]);
    expect(readLast()).toMatchObject({ symbol: "NQ=F" });
  });

  it("never passes 50 entries: the oldest go, the one just written stays, last stays", () => {
    activateTab("first", "NQ=F", "5m", null);
    for (let i = 0; i < 60; i += 1) {
      vi.advanceTimersByTime(1000);
      seedTab("tab-" + i, "5m", null);
      expect(tabKeys().length).toBeLessThanOrEqual(MAX_TABS);
    }
    expect(tabKeys()).toHaveLength(MAX_TABS);
    expect(tabKeys()).toContain("tab-59");
    expect(tabKeys()).not.toContain("first");
    expect(tabKeys()).not.toContain("tab-0");
    expect(readLast()).toMatchObject({ symbol: "NQ=F" });
  });
});
