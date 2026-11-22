import { describe, expect, it } from "vitest";
import { applyLast, applySnapshot, applyStatus, initialState } from "./liveState";
import { snapshot } from "@/test/fakeSocket";

const apply = (fields: Record<string, unknown>) => applySnapshot(initialState("QQQ", "5m"), snapshot("c1", 1, { symbol: "QQQ", tf: "5m", ...fields }) as never);

describe("tarea 3328: the header is the regular session and the after-hours trade goes aside", () => {
  it("reads the after-hours trade of the snapshot and of every price message, and clears it when the engine sends null", () => {
    const state = apply({ last: 608.45, change: -36.59, change_pct: -5.67, extended: { price: 617.74, change: 9.29, change_pct: 1.53 } });
    expect(state.price).toBe(608.45);
    expect(state.changePct).toBe(-5.67);
    expect(state.extended).toEqual({ price: 617.74, change: 9.29, changePct: 1.53 });
    expect(applyLast(state, { type: "last", price: 608.45, extended: null } as never).extended).toBeNull();
    expect(applyLast(state, { type: "last", price: 608.45 } as never).extended).toEqual(state.extended);
    expect(apply({ last: 504.4 }).extended).toBeNull();
    expect(apply({ extended: { change: 1 } }).extended).toBeNull();
  });

  it("a status carries the provider's answer: closed then open again, a connected link in both", () => {
    const open = apply({ market_open: true });
    const closed = applyStatus(open, { type: "status", state: "closed", market_open: false, data_state: "live" } as never);
    expect(closed.status).toBe("closed");
    expect(closed.marketOpen).toBe(false);
    expect(closed.dataState).toBe("live");
    const reopened = applyStatus(closed, { type: "status", state: "live", market_open: true, data_state: "live" } as never);
    expect(reopened.status).toBe("live");
    expect(reopened.marketOpen).toBe(true);
  });
});
