import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getRestartState, resetRestartForTests } from "@/api/restart";
import { setLocale, t } from "@/i18n";
import { SettingsModal } from "@/shell/SettingsModal";
import { useShell } from "@/shell/store";
import { renderWithApp } from "@/test/utils";
import { NewsSection } from "./NewsSection";
import { failure, mockApi } from "./testkit";
import type { NewsData, UpdateStateData } from "./types";
import { refreshUpdateStatus, resetUpdateStatusForTests } from "./updateStatus";

beforeEach(() => {
  resetUpdateStatusForTests();
  resetRestartForTests();
});
afterEach(() => {
  vi.useRealTimers();
  resetRestartForTests();
  vi.unstubAllGlobals();
});

/** Half a minute past the whole minute, so a slow test machine cannot tip it into the next minute. */
const minutesAgo = (n: number) => new Date(Date.now() - (n * 60 + 30) * 1000).toISOString();

function news(over: Partial<NewsData> = {}): NewsData {
  return {
    checked_at: minutesAgo(3),
    installed: { version: "0.0.6", installed_at: "2026-11-04T12:00:00Z" },
    changelog: [{ version: "0.0.6", body: "- Chat with photos\n- A <script>alert(1)</script> line" }],
    latest: { version: "0.0.6", notes: null, published_at: "2026-11-05T10:00:00Z" },
    latest_error: null,
    newer: false,
    ...over,
  };
}

const NEWER = news({ latest: { version: "0.0.7", notes: "- Faster charts", published_at: "2026-11-05T10:00:00Z" }, newer: true });

function state(over: Partial<UpdateStateData> = {}): UpdateStateData {
  return {
    version: "0.0.6", phase: "idle", stage: null, elapsed_s: null, stage_elapsed_s: null, error: null,
    target_version: null, checked_at: minutesAgo(3), newer: false, latest_version: "0.0.6", supervised: true,
    engine_log: "/data/logs/engine.log", ...over,
  };
}

const UPDATED = { status: "updated", from_version: "0.0.6", to_version: "0.0.7", restart_required: true, restarting: true };

describe.each(["es", "en"] as const)("Novedades (%s)", (locale) => {
  const T = (key: string, params?: Record<string, string | number>) => t(key, params, locale);

  it("U1: up to date shows the button and when it was checked; clicking it says 'Checking...' and then updates the time", async () => {
    setLocale(locale);
    let finish: (() => void) | undefined;
    mockApi({
      "GET /api/news": (call: { path: string }) =>
        call.path.includes("refresh=true")
          ? new Promise((resolve) => {
              finish = () => resolve(news({ checked_at: new Date().toISOString() }));
            })
          : news(),
      "GET /api/update/state": state(),
    });
    renderWithApp(<NewsSection />);
    const upToDate = (when: string) => T("news.upToDate", { version: "0.0.6" }) + " · " + T("news.checked", { when });
    expect(await screen.findByText(upToDate(T("news.ago.min", { n: 3 })), {}, { timeout: 4000 })).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: T("news.check") }));
    expect(await screen.findByRole("button", { name: T("news.checking") })).toBeDisabled();
    await waitFor(() => expect(finish).toBeDefined());
    await act(async () => finish!());
    expect(await screen.findByText(upToDate(T("news.ago.now")))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("news.check") })).toBeEnabled();
  });

  it("shows the new version with Update Hokma as the main action and Check for updates next to it", async () => {
    setLocale(locale);
    mockApi({ "GET /api/news": NEWER, "GET /api/update/state": state({ newer: true, latest_version: "0.0.7" }) });
    renderWithApp(<NewsSection />);
    expect(await screen.findByText(T("news.newer", { version: "0.0.7" }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("news.update") })).toBeEnabled();
    expect(screen.getByRole("button", { name: T("news.check") })).toBeEnabled();
    expect(screen.getByText("Faster charts")).toBeInTheDocument();
  });

  it("U2: update, then 'Restarting Hokma...' and the watch for the new version starts", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/news": NEWER,
      "GET /api/update/state": state({ newer: true }),
      "POST /api/update": UPDATED,
      "GET /health": { status: "ok", version: "0.0.6" },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    expect(await screen.findByText(T("news.restarting"))).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/update")).toBe(true);
    expect(screen.getByRole("button", { name: T("news.check") })).toBeDisabled();
  });

  it("task 3146: the update runs detached; the tab says 'Updating Hokma, it will restart by itself'", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/news": NEWER,
      "GET /api/update/state": state({ newer: true }),
      "POST /api/update": { status: "launched", from_version: "0.0.6", to_version: "0.0.7", restart_required: false, restarting: true },
      "GET /health": { status: "ok", version: "0.0.6" },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    expect(await screen.findByText(T("news.restarting"))).toBeInTheDocument();
    expect(getRestartState()).toMatchObject({ phase: "restarting", kind: "update" });
  });

  it("U7: without a supervisor it says to restart Hokma by hand", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/news": NEWER,
      "GET /api/update/state": state({ newer: true, supervised: false }),
      "POST /api/update": { ...UPDATED, restarting: false },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    expect(await screen.findByText(T("news.restartManual"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("news.check") })).toBeEnabled();
  });

  it("U3: a refused package shows the message, keeps the screen usable and Retry tries again", async () => {
    setLocale(locale);
    let attempts = 0;
    mockApi({
      "GET /api/news": NEWER,
      "GET /api/update/state": state({ newer: true }),
      "POST /api/update": () => (++attempts === 1 ? failure(502, "The package does not match") : UPDATED),
      "GET /health": { status: "ok", version: "0.0.6" },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    expect(await screen.findByText("The package does not match")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("news.check") })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: T("news.retry") }));
    expect(await screen.findByText(T("news.restarting"))).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it("when the search fails it says so, with Retry and Check for updates", async () => {
    setLocale(locale);
    mockApi({
      "GET /api/news": news({ latest: null, latest_error: "Offline" }),
      "GET /api/update/state": state(),
    });
    renderWithApp(<NewsSection />);
    expect(await screen.findByText(T("news.checkFailed", { message: "Offline" }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("news.retry") })).toBeEnabled();
    expect(screen.getByRole("button", { name: T("news.check") })).toBeEnabled();
  });

  it("shows the real stage of a running update and a clock that keeps counting", async () => {
    setLocale(locale);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi({
      "GET /api/news": NEWER,
      "GET /api/update/state": state({ phase: "updating", stage: "dependencies", elapsed_s: 7, newer: true }),
    });
    renderWithApp(<NewsSection />);
    const clock = await screen.findByTestId("update-clock");
    expect(clock).toHaveTextContent(T("news.elapsed", { time: "0:07" }));
    const items = Array.from(document.querySelectorAll("li[data-state]")).map((li) => [li.textContent, li.getAttribute("data-state")]);
    expect(items).toEqual([
      ["✓" + T("news.stage.download"), "done"],
      ["✓" + T("news.stage.unpack"), "done"],
      ["✓" + T("news.stage.python"), "done"],
      ["›" + T("news.stage.dependencies"), "now"],
      ["·" + T("news.stage.verify"), "next"],
      ["·" + T("news.stage.switch"), "next"],
    ]);
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await waitFor(() => expect(screen.getByTestId("update-clock")).toHaveTextContent(T("news.elapsed", { time: "0:10" })));
    expect(screen.getByRole("button", { name: T("news.check") })).toBeDisabled();
  });
});

describe("the dot in Settings", () => {
  function OpenNews() {
    const shell = useShell();
    useEffect(() => shell.openSettings("news"), []); // eslint-disable-line react-hooks/exhaustive-deps
    return <SettingsModal />;
  }

  it("shows a dot next to News when there is a new version, and no dot when there is none", async () => {
    mockApi({ "GET /api/update/state": state({ newer: true, latest_version: "0.0.7" }), "GET /api/news": NEWER });
    await refreshUpdateStatus();
    renderWithApp(<OpenNews />);
    const nav = await screen.findByRole("button", { name: t("settings.news") + ", " + t("news.dotLabel") });
    expect(nav.querySelector(".nav-badge")).not.toBeNull();
    expect(screen.getByRole("button", { name: t("settings.general") }).querySelector(".nav-badge")).toBeNull();
  });

  it("has no dot when the version is current", async () => {
    mockApi({ "GET /api/update/state": state(), "GET /api/news": news() });
    await refreshUpdateStatus();
    renderWithApp(<OpenNews />);
    expect(await screen.findByRole("button", { name: t("settings.news") })).toBeInTheDocument();
    expect(document.querySelector(".nav-badge")).toBeNull();
  });
});
