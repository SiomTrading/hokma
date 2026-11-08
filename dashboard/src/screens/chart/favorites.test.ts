import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_FAVORITES,
  INTERVALS_KEY,
  MAX_FAVORITES,
  RECENT_KEY,
  TIMEFRAME_KEY,
  isFavorite,
  loadFavorites,
  loadInterval,
  loadRecent,
  pushRecent,
  saveFavorites,
  saveInterval,
  toggleFavorite,
} from "./favorites";

afterEach(() => vi.restoreAllMocks());

describe("favorite intervals", () => {
  it("starts with 1m 5m 15m 1h 1D", () => {
    expect(loadFavorites()).toEqual(["1m", "5m", "15m", "1h", "1D"]);
    expect(DEFAULT_FAVORITES).toEqual(["1m", "5m", "15m", "1h", "1D"]);
  });

  it("drops invalid values, normalizes, removes repeats and keeps at most 8", () => {
    localStorage.setItem(INTERVALS_KEY, JSON.stringify(["60m", "1h", "bogus", 7, "3M", "10r", "r:2.50p", "15s", "2m", "3m", "4m", "6m", "7m", "8m"]));
    const list = loadFavorites();
    expect(list).toHaveLength(MAX_FAVORITES);
    expect(list).not.toContain("bogus");
    expect(list.filter((item) => item === "1h")).toHaveLength(1);
    expect(list[0]).toBe("15s");
  });

  it("reads an unreadable value as the defaults", () => {
    localStorage.setItem(INTERVALS_KEY, "{not json");
    expect(loadFavorites()).toEqual([...DEFAULT_FAVORITES]);
  });

  it("pins and unpins, saving each change, and refuses a ninth", () => {
    let list = toggleFavorite(loadFavorites(), "7m");
    expect(list).toEqual(["1m", "5m", "7m", "15m", "1h", "1D"]);
    expect(JSON.parse(localStorage.getItem(INTERVALS_KEY)!)).toEqual(list);
    expect(isFavorite(list, "420s")).toBe(true);
    list = toggleFavorite(list, "7m");
    expect(isFavorite(list, "7m")).toBe(false);
    list = saveFavorites(["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m"]);
    expect(toggleFavorite(list, "r:10t")).toEqual(list);
    expect(toggleFavorite(list, "nonsense")).toEqual(list);
  });
});

describe("recent symbols", () => {
  it("keeps the last 8 picked, newest first, without repeats", () => {
    for (const symbol of ["A", "B", "C", "D", "E", "F", "G", "H", "I"]) pushRecent(symbol);
    expect(loadRecent()).toEqual(["I", "H", "G", "F", "E", "D", "C", "B"]);
    pushRecent("e");
    expect(loadRecent()).toEqual(["e", "I", "H", "G", "F", "D", "C", "B"]);
    expect(JSON.parse(localStorage.getItem(RECENT_KEY)!)).toHaveLength(8);
  });

  it("ignores junk in storage", () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify(["NQ=F", 4, "", "has space", "nq=f", "BTC-USD"]));
    expect(loadRecent()).toEqual(["NQ=F", "BTC-USD"]);
  });
});

describe("active interval", () => {
  it("accepts the old values and the new ones of the grammar, without migration", () => {
    expect(loadInterval()).toBe("5m");
    localStorage.setItem(TIMEFRAME_KEY, "1D");
    expect(loadInterval()).toBe("1D");
    localStorage.setItem(TIMEFRAME_KEY, "r:10t");
    expect(loadInterval()).toBe("r:10t");
    localStorage.setItem(TIMEFRAME_KEY, "60m");
    expect(loadInterval()).toBe("1h");
    localStorage.setItem(TIMEFRAME_KEY, "3M");
    expect(loadInterval()).toBe("5m");
    saveInterval("2.5rp");
    expect(localStorage.getItem(TIMEFRAME_KEY)).toBe("r:2.5p");
    saveInterval("bogus");
    expect(localStorage.getItem(TIMEFRAME_KEY)).toBe("r:2.5p");
  });
});

describe("storage unavailable", () => {
  it("never throws and falls back to the defaults", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(loadFavorites()).toEqual([...DEFAULT_FAVORITES]);
    expect(loadRecent()).toEqual([]);
    expect(loadInterval()).toBe("5m");
    expect(() => saveInterval("7m")).not.toThrow();
    expect(() => pushRecent("AAPL")).not.toThrow();
    expect(toggleFavorite(loadFavorites(), "7m")).toContain("7m");
  });
});
