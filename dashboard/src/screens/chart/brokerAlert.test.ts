import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN } from "@/test/utils";
import { LiveChartClient } from "./liveClient";
import type { LiveState } from "./liveState";

describe("the broker alert on the chart (SPEC-016 3.8)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearToken();
    setToken(TEST_TOKEN);
    installFakeSockets();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shows the red band while the broker is lost, then the green one for five seconds", () => {
    const states: LiveState[] = [];
    const client = new LiveChartClient({ symbol: "QQQ", timeframe: "5m", onState: (s) => states.push(s) });
    client.start();
    const socket = FakeSocket.last();
    socket.serverOpen();
    const send = (message: Record<string, unknown>) => socket.serverSend({ epoch: "e1", ...message });
    send({ type: "broker_alert", state: "lost", source: "Schwab", message: "Connection with Schwab was lost. Data is not updating." });
    expect(states.at(-1)!.brokerAlert).toEqual({ state: "lost", source: "Schwab", message: "Connection with Schwab was lost. Data is not updating." });
    vi.advanceTimersByTime(20_000);
    expect(states.at(-1)!.brokerAlert?.state).toBe("lost");                // the red band does not go by itself
    send({ type: "broker_alert", state: "recovered", source: "Schwab", message: "Connection with Schwab recovered at 10:01." });
    expect(states.at(-1)!.brokerAlert?.state).toBe("recovered");
    vi.advanceTimersByTime(5001);
    expect(states.at(-1)!.brokerAlert).toBeNull();
    client.stop();
  });
});
