import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { useShell } from "@/shell/useShell";
import { errorKey } from "./api";
import { EnginesSettings } from "./EnginesSettings";
import { EnginesStep } from "./EnginesStep";
import { RegionCard } from "./RegionCard";

const LISTING = {
  connections: [] as unknown[],
  detect: {
    cli: { claude_code: true, codex: true, gemini_cli: true },
    codex_login: false,
    claude_import: false,
    providers: [
      { id: "openai", label: "OpenAI", wire: "openai", base_url: "https://api.openai.com", local: false },
      { id: "custom", label: "Custom", wire: "openai", base_url: "", local: false },
    ],
  },
  store_available: true,
  claude_disabled_by_user: false,
};

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

let pingBody: { ok: boolean; status: string; code: string; model?: string };
let loginBody: { state: string; url: string; code: string; reason?: string };
let codexTested = false;

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  pingBody = { ok: false, status: "error", code: "auth" };
  loginBody = { state: "waiting", url: "https://auth.example/device", code: "AB12-CD34E" };
  codexTested = false;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/claude")) return reply({ state: "connected", plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && method === "GET") return reply(LISTING);
      if (url.endsWith("/api/engines/api")) return reply({ id: 7, engine: "api", label: "OpenAI", status: "unknown", is_default: false });
      if (url.endsWith("/api/engines/7/test")) return reply(pingBody);
      if (url.endsWith("/api/engines/codex/login")) return reply(loginBody);
      if (url.endsWith("/api/engines/cli")) return reply({ id: 9, engine: "codex", label: "ChatGPT (Codex)", status: "unknown", is_default: false });
      if (url.endsWith("/api/engines/9/test")) {
        codexTested = true;
        return reply({ ok: false, status: "error", code: "workspace" });
      }
      return reply({}, 404);
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("connect your AI", () => {
  it("lists Claude Code first as recommended and always offers 'later'", async () => {
    const later = vi.fn();
    renderWithApp(<EnginesStep onLater={later} />);
    expect(await screen.findByText(t("eng.recommended"))).toBeInTheDocument();
    expect(screen.getByText(t("eng.codex.title"))).toBeInTheDocument();
    expect(screen.getByText(t("eng.gemini.title"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("eng.later") }));
    expect(later).toHaveBeenCalledOnce();
  });

  it("shows a clear error for a false API key", async () => {
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await screen.findByText(t("eng.key.title"));
    await userEvent.type(screen.getByLabelText(t("eng.key.label")), "sk-fake-123");
    await userEvent.click(screen.getByRole("button", { name: t("eng.key.connect") }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(t("eng.err.auth", { provider: "OpenAI" }));
    expect(document.body.textContent).not.toContain("sk-fake-123");
  });

  it("a model the key cannot use says so and names the model, not a generic error (bug 1173)", async () => {
    pingBody = { ok: false, status: "error", code: "model", model: "gemini-9-gone" };
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await screen.findByText(t("eng.key.title"));
    await userEvent.type(screen.getByLabelText(t("eng.key.label")), "sk-fake-123");
    await userEvent.click(screen.getByRole("button", { name: t("eng.key.connect") }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(t("eng.err.model", { provider: "OpenAI" }));
    expect(alert).toHaveTextContent("gemini-9-gone");
    expect(alert).not.toHaveTextContent(t("eng.err.engine_error"));
  });

  it("an unavailable provider says it is down for now (bug 1173)", async () => {
    pingBody = { ok: false, status: "error", code: "unavailable" };
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await screen.findByText(t("eng.key.title"));
    await userEvent.type(screen.getByLabelText(t("eng.key.label")), "sk-fake-123");
    await userEvent.click(screen.getByRole("button", { name: t("eng.key.connect") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t("eng.err.unavailable", { provider: "OpenAI" }));
  });

  it("a ChatGPT sign-in refused by the workspace says why, not a bad session (bug 1175)", async () => {
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("eng.codex.login") }));
    loginBody = { state: "failed", url: "", code: "", reason: "device_auth" };
    const alert = await screen.findByText(t("eng.err.device_auth", { provider: "ChatGPT" }), undefined, { timeout: 4000 });
    expect(alert).toBeInTheDocument();
  });

  it("after a ChatGPT sign-in the connection is tested and a workspace without Codex is reported (bug 1175)", async () => {
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: t("eng.codex.login") }));
    loginBody = { state: "done", url: "", code: "" };
    expect(await screen.findByText(t("eng.err.workspace", { provider: "ChatGPT (Codex)" }), undefined, { timeout: 4000 })).toBeInTheDocument();
    expect(codexTested).toBe(true);
  });

  it("shows the region card with another engine and no mention of a VPN", async () => {
    pingBody = { ok: false, status: "error", code: "region" };
    renderWithApp(<EnginesStep onLater={() => undefined} />);
    await screen.findByText(t("eng.key.title"));
    await userEvent.type(screen.getByLabelText(t("eng.key.label")), "sk-fake-123");
    await userEvent.click(screen.getByRole("button", { name: t("eng.key.connect") }));
    expect(await screen.findByText(t("eng.region.title", { provider: "OpenAI" }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.region.other") })).toBeInTheDocument();
  });
});

describe("engines screens", () => {
  it("the region texts never name a VPN, in both languages", () => {
    for (const lang of ["es", "en"] as const) {
      setLocale(lang);
      for (const key of ["eng.region.title", "eng.region.body", "eng.err.region"]) {
        expect(t(key, { provider: "X" })).not.toMatch(/vpn/i);
      }
    }
  });

  it("RegionCard offers retry and another engine", async () => {
    const other = vi.fn();
    renderWithApp(<RegionCard provider="OpenAI" onRetry={() => undefined} onOther={other} />);
    await userEvent.click(screen.getByRole("button", { name: t("eng.region.other") }));
    expect(other).toHaveBeenCalledOnce();
  });

  it("Settings > Engines shows the Claude sign-in when the CLI has no session (bug 1112)", async () => {
    const base = fetch as unknown as (i: RequestInfo | URL, n?: RequestInit) => Promise<Response>;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/claude")) return reply({ state: "no_session", plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && (init?.method ?? "GET") === "GET") {
        return reply({ ...LISTING, connections: [{ id: 1, engine: "claude_code", label: "Claude Code", status: "connected", status_detail: null, is_default: true }] });
      }
      return base(input, init);
    }));
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByRole("button", { name: t("firstrun.claude.login") })).toBeInTheDocument();
  });

  it("Settings > Engines with no connection shows the banner and a way to connect", async () => {
    renderWithApp(<EnginesSettings />);
    await waitFor(() => expect(screen.getByText(t("eng.none_banner"))).toBeInTheDocument());
    expect(screen.getByRole("button", { name: t("eng.connectAnother") })).toBeInTheDocument();
  });
});

describe("Claude is always listed in Settings > Engines (bug 1123)", () => {
  function answer(claude: Record<string, unknown>, connections: unknown[], calls: string[] = []) {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") calls.push(`${method} ${url.replace(/^.*\/api/, "/api")}`);
      if (url.endsWith("/api/claude")) return reply({ plan: null, login: { url: null, error: null }, ...claude });
      if (url.endsWith("/api/claude/login")) return reply({ running: true, url: null });
      if (url.endsWith("/api/engines") && method === "GET") return reply({ ...LISTING, connections });
      if (url.endsWith("/api/engines/claude")) return reply({ id: 5, engine: "claude_code", label: "Claude", status: "connected", is_default: false });
      return reply({}, 404);
    }));
    return calls;
  }

  it("with no saved connection and a working session it says Connected and restores the saved connection (bug 1124)", async () => {
    const calls = answer({ state: "connected", installed: true }, []);
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.connected"))).toBeInTheDocument();
    expect(screen.queryByText(t("eng.claude.state.disconnected"))).not.toBeInTheDocument();
    await waitFor(() => expect(calls).toContain("POST /api/engines/claude"));
  });

  /** A server that keeps the Disconnect the way the real one does: the flag is on while there is only a removed Claude row. */
  function serverWithFlag(initial: { rows: unknown[]; disabled: boolean }, calls: string[]) {
    const state = { ...initial };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") calls.push(`${method} ${url.replace(/^.*\/api/, "/api")}`);
      if (url.endsWith("/api/claude")) return reply({ state: "connected", installed: true, plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines/1") && method === "DELETE") {
        state.rows = [];
        state.disabled = true;
        return reply({ ok: true });
      }
      if (url.endsWith("/api/engines") && method === "GET") return reply({ ...LISTING, connections: state.rows, claude_disabled_by_user: state.disabled });
      if (url.endsWith("/api/engines/claude")) {
        state.rows = [{ id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: false }];
        state.disabled = false;
        return reply(state.rows[0]);
      }
      return reply({}, 404);
    }));
  }

  it("after Disconnect the card says the truth and Connect brings it back (the flag comes from the server)", async () => {
    const calls: string[] = [];
    serverWithFlag({
      rows: [{ id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: true }],
      disabled: false,
    }, calls);
    renderWithApp(<EnginesSettings />);
    await userEvent.click(await screen.findByRole("button", { name: t("eng.disconnect") }));
    expect(await screen.findByText(t("eng.claude.state.disconnected"))).toBeInTheDocument();
    expect(screen.getByText(t("eng.claude.disconnectedBody"))).toBeInTheDocument();
    expect(calls).toEqual(["DELETE /api/engines/1"]);            // no restoration POST although the CLI session still works
    await userEvent.click(screen.getByRole("button", { name: t("eng.claude.connect") }));
    await waitFor(() => expect(calls).toContain("POST /api/engines/claude"));
    expect(await screen.findByText(t("eng.claude.state.connected"))).toBeInTheDocument();
    expect(screen.queryByText(t("eng.claude.state.disconnected"))).not.toBeInTheDocument();
  });

  it("the Disconnect survives leaving Settings and coming back, and nothing is restored by itself", async () => {
    const calls: string[] = [];
    serverWithFlag({ rows: [], disabled: true }, calls);
    const first = renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.disconnected"))).toBeInTheDocument();
    first.unmount();
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.disconnected"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.claude.connect") })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).toEqual([]);
    expect(screen.queryByText(t("eng.claude.state.connected"))).not.toBeInTheDocument();
  });

  it("only the engine the chat really uses (data.active.id) carries the Active pill (bug 1128)", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/claude")) return reply({ state: "connected", installed: true, plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && (init?.method ?? "GET") === "GET") {
        return reply({
          ...LISTING,
          active: { id: 2, engine: "gemini_cli", label: "Gemini", replaced: true, available: true, saved: "Claude" },
          connections: [
            { id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: true },
            { id: 2, engine: "gemini_cli", label: "Gemini", auth_kind: "subscription", status: "connected", status_detail: null, is_default: false },
          ],
        });
      }
      return reply({}, 404);
    }));
    renderWithApp(<EnginesSettings />);
    await screen.findByText("Gemini");
    await screen.findAllByText(t("eng.claude.state.connected"));
    expect(screen.getAllByText(t("eng.active"))).toHaveLength(1);
    expect(within(screen.getByTestId("claude-card")).queryByText(t("eng.active"))).not.toBeInTheDocument();
  });

  it("a Claude check that could not run says so, offers Check again and never shows the sign-in (bug 1128)", async () => {
    let state = "unknown";
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/claude")) return reply({ state, installed: true, plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && (init?.method ?? "GET") === "GET") return reply({ ...LISTING, connections: [] });
      return reply({}, 404);
    }));
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.unknown"))).toBeInTheDocument();
    expect(screen.queryByText(t("eng.claude.state.expired"))).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("firstrun.claude.login") })).not.toBeInTheDocument();
    state = "connected";
    await userEvent.click(screen.getByRole("button", { name: t("eng.claude.recheck") }));
    expect(await screen.findByText(t("eng.claude.state.connected"))).toBeInTheDocument();
  });

  it("when reading the Claude state fails the card shows 'could not check', not 'Checking' forever", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/claude")) return reply({ detail: "boom" }, 500);
      if (url.endsWith("/api/engines") && (init?.method ?? "GET") === "GET") return reply({ ...LISTING, connections: [] });
      return reply({}, 404);
    }));
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.unknown"))).toBeInTheDocument();
    expect(screen.queryByText(t("firstrun.claude.checking"))).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.claude.recheck") })).toBeInTheDocument();
  });

  it("the pills of the other engines use the catalog, not the raw status", async () => {
    answer({ state: "connected", installed: true }, [
      { id: 3, engine: "codex", label: "Codex", auth_kind: "subscription", status: "no_session", status_detail: null, is_default: false },
      { id: 4, engine: "gemini_cli", label: "Gemini", auth_kind: "subscription", status: "no_cli", status_detail: null, is_default: false },
      { id: 5, engine: "api", label: "OpenAI", auth_kind: "api_key", status: "weird_new_status", status_detail: null, is_default: false },
    ]);
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.status.no_session"))).toBeInTheDocument();
    expect(screen.getByText(t("eng.status.no_cli"))).toBeInTheDocument();
    expect(screen.queryByText("no_session")).not.toBeInTheDocument();
    expect(screen.getByText("weird_new_status")).toBeInTheDocument();      // an unknown status is still shown, never hidden
  });

  it("with an expired session and no saved connection it says so and offers the sign-in button", async () => {
    const calls = answer({ state: "no_session", installed: true }, []);
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.expired"))).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: t("firstrun.claude.login") }));
    await waitFor(() => expect(calls).toContain("POST /api/claude/login"));
  });

  it("when a check finds the session working the pill changes and Claude becomes a saved connection", async () => {
    const calls: string[] = [];
    let signedIn = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") calls.push(`${method} ${url.replace(/^.*\/api/, "/api")}`);
      if (url.endsWith("/api/claude")) return reply({ state: signedIn ? "connected" : "no_session", installed: true, plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && method === "GET") return reply({ ...LISTING, connections: [] });
      if (url.endsWith("/api/engines/claude")) return reply({ id: 5, engine: "claude_code", label: "Claude", status: "connected", is_default: false });
      return reply({}, 404);
    }));
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.expired"))).toBeInTheDocument();
    signedIn = true;
    await userEvent.click(await screen.findByRole("button", { name: t("firstrun.claude.recheck") }));
    await waitFor(() => expect(calls).toContain("POST /api/engines/claude"));
    await waitFor(() => expect(screen.queryByText(t("eng.claude.state.expired"))).not.toBeInTheDocument());
  });

  it("when Claude Code is not installed it still lists Claude with that status", async () => {
    answer({ state: "no_cli", installed: false }, []);
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("eng.claude.state.notInstalled"))).toBeInTheDocument();
    expect(screen.getByTestId("claude-card")).toBeInTheDocument();
  });

  it("a connected Claude shows its status with the plan and the Test and Disconnect buttons", async () => {
    answer({ state: "connected", installed: true, plan: "Max" }, [
      { id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: true },
    ]);
    renderWithApp(<EnginesSettings />);
    expect(await screen.findByText(t("firstrun.claude.connectedPlan", { plan: "Max" }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.test") })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.disconnect") })).toBeInTheDocument();
  });

  it("the Connect your AI banner shows only when no engine can answer (1162)", async () => {
    const listing = (available: boolean) => ({
      ...LISTING,
      connections: [{ id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: true }],
      active: { id: 1, engine: "claude_code", label: "Claude", replaced: false, available, saved: null },
    });
    for (const available of [true, false]) {
      vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/claude")) return reply({ state: "connected", installed: true, plan: "Max", login: { running: false, url: null } });
        if (url.endsWith("/api/engines")) return reply(listing(available));
        return reply({}, 404);
      }));
      const { unmount } = renderWithApp(<EnginesSettings />);
      await screen.findByTestId("claude-card");
      if (available) expect(screen.queryByText(t("eng.none_banner"))).not.toBeInTheDocument();
      else expect(screen.getByText(t("eng.none_banner"))).toBeInTheDocument();
      unmount();
    }
  });

  it("the Test button of Claude toasts the time on success and the reason on failure (1162)", async () => {
    function Toasts() {
      const shell = useShell();
      return <ul>{shell.toasts.map((item) => <li key={item.id}>{item.text}</li>)}</ul>;
    }
    for (const ok of [true, false]) {
      vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/claude")) return reply({ state: "connected", installed: true, plan: "Max", login: { running: false, url: null } });
        if (url.endsWith("/api/engines/1/test")) return reply(ok ? { ok: true, status: "connected", code: "" } : { ok: false, status: "error", code: "claude_not_found" });
        if (url.endsWith("/api/engines")) {
          return reply({ ...LISTING, connections: [{ id: 1, engine: "claude_code", label: "Claude", auth_kind: "subscription", status: "connected", status_detail: null, is_default: true }] });
        }
        return reply({}, 404);
      }));
      const { unmount } = renderWithApp(<><Toasts /><EnginesSettings /></>);
      await userEvent.click(await screen.findByRole("button", { name: t("eng.test") }));
      if (ok) expect(await screen.findByText(/Claude responde \(\d+\.\d s\)/)).toBeInTheDocument();
      else expect(await screen.findByText(t("eng.claude.testFail", { detail: t("eng.err.not_found", { cli: "Claude" }) }))).toBeInTheDocument();
      unmount();
    }
  });

  it("Gemini is disabled by the real detect key gemini_cli and says it cannot find it (1158)", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/claude")) return reply({ state: "connected", installed: true, plan: null, login: { running: false, url: null } });
      if (url.endsWith("/api/engines")) return reply({ ...LISTING, detect: { ...LISTING.detect, cli: { claude_code: true, codex: true, gemini_cli: false } } });
      return reply({}, 404);
    }));
    renderWithApp(<EnginesStep onLater={() => {}} />);
    expect(await screen.findByText(t("eng.err.not_found", { cli: "Gemini" }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("eng.gemini.connect") })).toBeDisabled();
  });

  it("every adapter code ending in _not_found reads as not found (1158)", () => {
    for (const code of ["claude_not_found", "codex_not_found", "gemini_not_found", "not_found"]) expect(errorKey(code)).toBe("eng.err.not_found");
    expect(errorKey("auth")).toBe("eng.err.auth");
    expect(errorKey("model")).toBe("eng.err.model");
    expect(errorKey("unavailable")).toBe("eng.err.unavailable");
    expect(errorKey("weird")).toBe("eng.err.engine_error");
  });

  it("the texts exist in both languages", () => {
    for (const lang of ["es", "en"] as const) {
      setLocale(lang);
      for (const key of ["connected", "disconnected", "expired", "notInstalled", "unknown"]) {
        expect(t(`eng.claude.state.${key}`)).not.toBe(`eng.claude.state.${key}`);
      }
      expect(t("eng.claude.connect")).not.toBe("eng.claude.connect");
      for (const key of ["eng.claude.testOk", "eng.claude.testFail", "eng.claude.recheck", "eng.claude.unknownBody", "eng.claude.disconnectedBody"]) expect(t(key)).not.toBe(key);
      for (const status of ["connected", "ok", "error", "unknown", "no_session", "no_cli", "removed"]) {
        expect(t(`eng.status.${status}`)).not.toBe(`eng.status.${status}`);
      }
      expect(t("eng.claude.disconnectedBody")).not.toMatch(/\u2014/);
    }
  });
});
