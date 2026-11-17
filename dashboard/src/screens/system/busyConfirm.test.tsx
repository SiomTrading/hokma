import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetRestartForTests } from "@/api/restart";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { NewsSection } from "./NewsSection";
import { ShutdownDialog } from "./ShutdownDialog";
import { mockApi } from "./testkit";
import { resetUpdateStatusForTests } from "./updateStatus";

beforeEach(() => {
  resetUpdateStatusForTests();
  resetRestartForTests();
});
afterEach(() => {
  resetRestartForTests();
  vi.unstubAllGlobals();
});

const inUse = (code: string, message: string) =>
  new Response(JSON.stringify({ detail: { code, message } }), { status: 409, headers: { "Content-Type": "application/json" } });

describe.each(["es", "en"] as const)("bug 1135 (%s)", (locale) => {
  const T = (key: string) => t(key, undefined, locale);

  it("shutdown: warns when /health says an agent is working and continues with force=true", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /health": { status: "ok", version: "0.0.36", busy: true },
      "POST /api/system/shutdown": { stopping: true },
    });
    renderWithApp(<ShutdownDialog withSession onClose={() => {}} />);
    expect(await screen.findByTestId("shutdown-busy")).toHaveTextContent(T("busy.warning"));
    await userEvent.click(screen.getByTestId("shutdown-confirm"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.path).toBe("/api/system/shutdown?force=true");
  });

  it("shutdown: no warning and no force when nothing runs", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /health": { status: "ok", version: "0.0.36", busy: false },
      "POST /api/system/shutdown": { stopping: true },
    });
    renderWithApp(<ShutdownDialog withSession onClose={() => {}} />);
    await waitFor(() => expect(calls.some((c) => c.path === "/health")).toBe(true));
    expect(screen.queryByTestId("shutdown-busy")).toBeNull();
    await userEvent.click(screen.getByTestId("shutdown-confirm"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.path).toBe("/api/system/shutdown");
  });

  it("shutdown: an agent that starts after the dialog opened turns the refusal into the warning", async () => {
    setLocale(locale);
    let first = true;
    const calls = mockApi({
      "GET /health": { status: "ok", version: "0.0.36", busy: false },
      "POST /api/system/shutdown": () => {
        if (first) {
          first = false;
          return inUse("shutdown.in_use", "busy");
        }
        return { stopping: true };
      },
    });
    renderWithApp(<ShutdownDialog withSession onClose={() => {}} />);
    await waitFor(() => expect(calls.some((c) => c.path === "/health")).toBe(true));
    await userEvent.click(screen.getByTestId("shutdown-confirm"));
    expect(await screen.findByTestId("shutdown-busy")).toHaveTextContent(T("busy.warning"));
    await userEvent.click(screen.getByTestId("shutdown-confirm"));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "POST")[1].path).toBe("/api/system/shutdown?force=true");
  });

  it("update: the refusal opens the warning and Continue repeats the call with force=true", async () => {
    setLocale(locale);
    let first = true;
    const calls = mockApi({
      "GET /api/news": {
        checked_at: new Date().toISOString(),
        installed: { version: "0.0.6", installed_at: "2026-11-04T12:00:00Z" },
        changelog: [],
        latest: { version: "0.0.7", notes: "- x", published_at: "2026-11-05T10:00:00Z" },
        latest_error: null,
        newer: true,
      },
      "GET /api/update/state": { version: "0.0.6", phase: "idle", newer: true, latest_version: "0.0.7", supervised: true },
      "GET /health": { status: "ok", version: "0.0.6" },
      "POST /api/update": () => {
        if (first) {
          first = false;
          return inUse("update.in_use", "busy");
        }
        return { status: "launched", from_version: "0.0.6", to_version: "0.0.7", restart_required: false, restarting: true };
      },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    expect(await screen.findByTestId("busy-warning")).toHaveTextContent(T("busy.warning"));
    await userEvent.click(screen.getByTestId("busy-continue"));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/api/update", "/api/update?force=true"]);
  });

  it("update: keeping the work cancels without a second call", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/news": {
        checked_at: new Date().toISOString(),
        installed: { version: "0.0.6", installed_at: "2026-11-04T12:00:00Z" },
        changelog: [],
        latest: { version: "0.0.7", notes: "- x", published_at: "2026-11-05T10:00:00Z" },
        latest_error: null,
        newer: true,
      },
      "GET /api/update/state": { version: "0.0.6", phase: "idle", newer: true, latest_version: "0.0.7", supervised: true },
      "POST /api/update": () => inUse("update.in_use", "busy"),
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: T("news.update") }));
    await screen.findByTestId("busy-warning");
    await userEvent.click(screen.getByRole("button", { name: T("busy.keep") }));
    await waitFor(() => expect(screen.queryByTestId("busy-warning")).toBeNull());
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});
