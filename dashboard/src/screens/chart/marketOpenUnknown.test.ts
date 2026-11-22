import { describe, expect, it } from "vitest";
import { applyStatus, applySnapshot, initialState, toChartData } from "./liveState";
import { snapshot } from "@/test/fakeSocket";

const apply = (fields: Record<string, unknown>) => applySnapshot(initialState("QQQ", "5m"), snapshot("c1", 1, { symbol: "QQQ", tf: "5m", ...fields }) as never);

describe("market open is a value of three: open, closed, unknown", () => {
  it("keeps what the engine says and never turns an unknown into open or closed", () => {
    expect(apply({ market_open: true }).marketOpen).toBe(true);
    expect(apply({ market_open: false }).marketOpen).toBe(false);
    expect(apply({ market_open: null }).marketOpen).toBeNull();
    expect(initialState("QQQ", "5m").marketOpen).toBeNull();
    expect(toChartData(apply({ market_open: null })).market_open).toBeNull();
  });

  it("a status of the engine only closes it; it does not open an unknown market", () => {
    const unknown = apply({ market_open: null });
    expect(applyStatus(unknown, { type: "status", state: "live" } as never).marketOpen).toBeNull();
    expect(applyStatus(unknown, { type: "status", state: "closed" } as never).marketOpen).toBe(false);
    expect(applyStatus(apply({ market_open: true }), { type: "status", state: "reconnecting" } as never).marketOpen).toBe(true);
  });
});
