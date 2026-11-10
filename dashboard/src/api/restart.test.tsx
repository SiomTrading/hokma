import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, getToken, openSocket } from "@/api";
import { RestartOverlay } from "@/screens/system/RestartOverlay";
import { setLocale, t } from "@/i18n";
import { GIVE_UP_MS, getRestartState, learnEngineVersion, rememberLogPath, resetRestartForTests, takeJustUpdated, watchEngine } from "./restart";
import { setToken } from "./token";

const TOKEN = "abcDEF1234567890_-xyz";

/** `/health` answers from a script, one entry per call: a version, or null for "not reachable". */
function health(script: Array<string | null>) {
  let i = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const next = script[Math.min(i++, script.length - 1)];
      if (next === null) throw new TypeError("refused");
      return new Response(JSON.stringify({ status: "ok", version: next }), { status: 200 });
    }),
  );
}

async function seconds(n: number) {
  for (let i = 0; i < n; i++) await act(async () => void (await vi.advanceTimersByTimeAsync(1000)));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetRestartForTests();
  sessionStorage.clear();
});
afterEach(() => {
  resetRestartForTests();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("watching the engine", () => {
  it("restart: waits for the new version, then reloads and remembers what it installed", async () => {
    health([null, null, "0.0.6", "0.0.7"]);
    const reload = vi.fn();
    watchEngine({ mode: "restart", expectedVersion: "0.0.7", reload });
    expect(getRestartState().phase).toBe("restarting");
    await seconds(3);
    expect(reload).not.toHaveBeenCalled();                      // the old version still answers: not done yet
    await seconds(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(takeJustUpdated()).toBe("0.0.7");
    expect(takeJustUpdated()).toBeNull();                       // it is shown once
  });

  it("restart without a known version is over when the engine went down and came back", async () => {
    health(["0.0.6", null, "0.0.7"]);
    const reload = vi.fn();
    watchEngine({ mode: "restart", reload });
    await seconds(2);
    expect(reload).not.toHaveBeenCalled();
    await seconds(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("gives up after 60 seconds and names the log", async () => {
    health([null]);
    rememberLogPath("/data/logs/engine.log");
    watchEngine({ mode: "restart", expectedVersion: "0.0.7", reload: vi.fn() });
    await seconds(GIVE_UP_MS / 1000 - 1);
    expect(getRestartState().phase).toBe("restarting");
    await seconds(2);
    expect(getRestartState()).toEqual({ phase: "failed", logPath: "/data/logs/engine.log" });
  });

  it("a dropped socket is silent when the engine comes back, with no reload", async () => {
    health([null, "0.0.6"]);
    const reload = vi.fn();
    watchEngine({ mode: "crash", reload });
    expect(getRestartState().phase).toBe("idle");
    await seconds(3);
    expect(getRestartState().phase).toBe("idle");
    expect(reload).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(2);                     // and it stopped asking
  });

  it("a dropped socket whose engine comes back on ANOTHER version reloads the page (no stale code)", async () => {
    health(["0.0.12", null, "0.0.13"]);
    await act(async () => void (await learnEngineVersion()));
    const reload = vi.fn();
    watchEngine({ mode: "crash", reload });
    await seconds(3);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem("hokma.updated")).toBe("0.0.13");
  });

  it("the same version after a dropped socket does not reload", async () => {
    health(["0.0.12", null, "0.0.12"]);
    await act(async () => void (await learnEngineVersion()));
    const reload = vi.fn();
    watchEngine({ mode: "crash", reload });
    await seconds(3);
    expect(reload).not.toHaveBeenCalled();
    expect(getRestartState().phase).toBe("idle");
  });

  it("a dropped socket that never recovers ends in 'could not start'", async () => {
    health([null]);
    watchEngine({ mode: "crash", reload: vi.fn() });
    await seconds(61);
    expect(getRestartState().phase).toBe("failed");
  });

  it("repeated calls do not restart the 60 seconds", async () => {
    health([null]);
    watchEngine({ mode: "crash", reload: vi.fn() });
    await seconds(40);
    watchEngine({ mode: "crash", reload: vi.fn() });            // another socket retried and closed again
    await seconds(21);
    expect(getRestartState().phase).toBe("failed");
  });
});

describe("sockets", () => {
  class FakeSocket {
    static instances: FakeSocket[] = [];
    readyState = 1;
    onclose: ((e: { code: number; reason: string }) => void) | null = null;
    onopen = null;
    onmessage = null;
    onerror = null;
    constructor(public url: string) {
      FakeSocket.instances.push(this);
    }
    send() {}
    close() {}
  }

  beforeEach(() => {
    FakeSocket.instances = [];
    clearToken();
    setToken(TOKEN);
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  it("a close with the reason 'restarting' shows 'Restarting Hokma...' at once", () => {
    health([null]);
    openSocket("/ws/chat");
    FakeSocket.instances[0]!.onclose?.({ code: 1012, reason: "restarting" });
    expect(getRestartState().phase).toBe("restarting");
    expect(getToken()).toBe(TOKEN);                              // the same token still works afterwards
  });

  it("a close without a reason shows nothing yet", () => {
    health(["0.0.6"]);
    openSocket("/ws/chat");
    FakeSocket.instances[0]!.onclose?.({ code: 1006, reason: "" });
    expect(getRestartState().phase).toBe("idle");
  });
});

describe("the overlay", () => {
  it("says 'Restarting Hokma...' and then 'Hokma could not start' with the log path", async () => {
    setLocale("en");
    health([null]);
    rememberLogPath("/data/logs/engine.log");
    render(<RestartOverlay />);
    expect(screen.queryByRole("status")).toBeNull();
    act(() => watchEngine({ mode: "restart", expectedVersion: "0.0.7", reload: vi.fn() }));
    expect(screen.getByText(t("restart.title", undefined, "en"))).toBeInTheDocument();
    await seconds(61);
    expect(screen.getByText("Hokma could not start")).toBeInTheDocument();
    expect(screen.getByText(/\/data\/logs\/engine\.log/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));       // it watches again
    expect(screen.getByText("Restarting Hokma...")).toBeInTheDocument();
  });
});
