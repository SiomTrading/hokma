import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { NativeBand } from "./NativeBand";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

function fakeEngine(native: unknown, retry: () => Response = () => json({ restarting: true })) {
  const calls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    calls.push(`${init?.method ?? "GET"} ${url.pathname}${url.search}`);
    if (url.pathname === "/health") return json({ status: "ok", version: "1.0.4", busy: false, native });
    if (url.pathname === "/api/native/retry") return retry();
    return json({}, 404);
  });
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe.each(["es", "en"] as const)("native parts strip (%s)", (locale) => {
  beforeEach(() => {
    setLocale(locale);
    setToken("test-session-token-0123456789");
  });

  it("is hidden when nothing is off", async () => {
    const calls = fakeEngine({ safe_mode: false, off: [], notices: [] });
    render(<NativeBand />);
    await waitFor(() => expect(calls).toContain("GET /health"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId("native-band")).not.toBeInTheDocument();
  });

  it("names the part that is off and what Hokma does without it", async () => {
    fakeEngine({ safe_mode: false, off: ["embedder"], notices: [] });
    render(<NativeBand />);
    const band = await screen.findByTestId("native-band");
    expect(band).toHaveTextContent(t("native.part.embedder"));
    expect(band).toHaveTextContent(t("native.effect.embedder"));
    expect(screen.getByRole("button", { name: t("native.band.retry") })).toBeInTheDocument();
  });

  it("says it when Hokma runs in safe mode", async () => {
    fakeEngine({ safe_mode: true, off: [], notices: [] });
    render(<NativeBand />);
    expect(await screen.findByTestId("native-band-safe")).toHaveTextContent(t("native.band.safe"));
  });

  it("Try again asks the engine to clear its answers and restart", async () => {
    const calls = fakeEngine({ safe_mode: false, off: ["voice"], notices: [] });
    render(<NativeBand />);
    await userEvent.click(await screen.findByTestId("native-band-retry"));
    await waitFor(() => expect(calls).toContain("POST /api/native/retry"));
  });

  it("asks before cutting a working agent, then sends force", async () => {
    let first = true;
    const calls = fakeEngine({ safe_mode: false, off: ["voice"], notices: [] }, () => {
      if (first) {
        first = false;
        return json({ detail: { code: "native.in_use", message: "busy" } }, 409);
      }
      return json({ restarting: true });
    });
    render(<NativeBand />);
    await userEvent.click(await screen.findByTestId("native-band-retry"));
    await userEvent.click(await screen.findByTestId("busy-continue"));
    await waitFor(() => expect(calls).toContain("POST /api/native/retry?force=true"));
  });
});
