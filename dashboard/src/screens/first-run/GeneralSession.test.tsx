import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, getToken, setToken } from "@/api";
import { createDeviceKey, saveDeviceKey } from "@/api/devicekey";
import { resetSessionForTests } from "@/api/session";
import { t } from "@/i18n";
import { ShellProvider } from "@/shell/store";
import { installBrowserKeys, storedDeviceRecord } from "@/test/fakeBrowserKeys";
import type { FakeIndexedDb } from "@/test/fakeBrowserKeys";
import { GeneralSettings } from "./GeneralSettings";

const TOKEN = "test-session-token-0123456789";
const CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36";
const FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0";

const SETUP = {
  completed: true,
  profile: {
    owner_name: "Ana Torres",
    fund_name: null,
    language: "es",
    theme: "dark",
    workdir: "/home/ana/Hokma",
    database_engine: "sqlite",
    claude_status: "connected",
    claude_plan: "Max",
    setup_completed_at: "2026-11-01 10:00:00",
  },
  owner: { owner_name: "Ana Torres", fund_name: "" },
  workdir: { path: "/home/ana/Hokma", exists: true, git: true, manual: true, options: [] },
  database: { engine: "sqlite", location: null },
  agents: { total: 0 },
  claude: { state: "connected", plan: "Max" },
  version: "0.0.6",
};

const calls: Array<{ method: string; path: string; headers: Record<string, string> }> = [];
let logoutStatus = 200;

const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }));

function backend(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = new URL(String(input), "http://localhost").pathname;
  const method = init?.method ?? "GET";
  calls.push({ method, path, headers: (init?.headers ?? {}) as Record<string, string> });
  switch (`${method} ${path}`) {
    case "GET /api/setup":
      return reply(SETUP);
    case "GET /api/protections":
      return reply({ always_on: true, rules: [] });
    case "GET /api/profile/permission-mode":
      return reply({ mode: "ask", intro_seen: true });
    case "POST /api/session/logout":
      return logoutStatus === 200
        ? reply({ logged_out: true })
        : reply({ detail: { code: "owner_confirmation_required", message: "Only the owner" } }, logoutStatus);
    default:
      return reply({ detail: "unexpected" }, 404);
  }
}

function renderGeneral() {
  return render(
    <ShellProvider>
      <div className="hk">
        <GeneralSettings />
      </div>
    </ShellProvider>,
  );
}

let idb: FakeIndexedDb;

beforeEach(() => {
  calls.length = 0;
  logoutStatus = 200;
  resetSessionForTests();
  vi.stubGlobal("fetch", vi.fn(backend));
  idb = installBrowserKeys();
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(CHROME);
  setToken(TOKEN);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetSessionForTests();
  clearToken();
});

describe("Settings > General > session of this browser", () => {
  it("closes the session with the token and the owner confirmation and forgets the token", async () => {
    const user = userEvent.setup();
    renderGeneral();
    await user.click(await screen.findByRole("button", { name: t("session.logout.button") }));
    await waitFor(() => expect(getToken()).toBeNull());
    const logout = calls.find((c) => c.path === "/api/session/logout")!;
    expect(logout.method).toBe("POST");
    expect(logout.headers["X-Hokma-Token"]).toBe(TOKEN);
    expect(logout.headers["X-Hokma-Owner"]).toMatch(/^[0-9a-f]{32}$/);
  });

  it("keeps the session and says so when the engine refuses", async () => {
    logoutStatus = 403;
    const user = userEvent.setup();
    renderGeneral();
    await user.click(await screen.findByRole("button", { name: t("session.logout.button") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only the owner");
    expect(getToken()).toBe(TOKEN);
  });

  it("does not show the Firefox notice in Chrome", async () => {
    renderGeneral();
    await screen.findByRole("button", { name: t("session.logout.button") });
    expect(screen.queryByTestId("session-firefox")).not.toBeInTheDocument();
  });

  it("shows the notice to use another browser in Firefox", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(FIREFOX);
    renderGeneral();
    expect(await screen.findByTestId("session-firefox")).toHaveTextContent(t("session.firefoxNotice"));
  });

  it("deletes the device key of the browser when the session is closed", async () => {
    const created = await createDeviceKey();
    expect(await saveDeviceKey(created.pair)).toBe(true);
    expect(storedDeviceRecord(idb)).toBeDefined();
    const user = userEvent.setup();
    renderGeneral();
    await user.click(await screen.findByRole("button", { name: t("session.logout.button") }));
    await waitFor(() => expect(getToken()).toBeNull());
    expect(storedDeviceRecord(idb)).toBeUndefined();
  });

  it("does not show the notice about the key where the browser can keep it", async () => {
    renderGeneral();
    await screen.findByRole("button", { name: t("session.logout.button") });
    expect(screen.queryByTestId("session-no-storage")).not.toBeInTheDocument();
  });

  it("tells the owner that Hokma will not remember them where the browser cannot keep the key", async () => {
    vi.stubGlobal("indexedDB", undefined);
    renderGeneral();
    expect(await screen.findByTestId("session-no-storage")).toHaveTextContent(t("session.noStorage"));
    expect(t("session.noStorage")).toMatch(/^Este navegador no permite guardar .* Hokma no te recordar./);
  });

  it("shows the Firefox notice and not this one in Firefox", async () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(FIREFOX);
    vi.stubGlobal("indexedDB", undefined);
    renderGeneral();
    expect(await screen.findByTestId("session-firefox")).toBeInTheDocument();
    expect(screen.queryByTestId("session-no-storage")).not.toBeInTheDocument();
  });
});
