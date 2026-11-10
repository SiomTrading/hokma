import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { EnginesSettings } from "./EnginesSettings";
import { EnginesStep } from "./EnginesStep";
import { RegionCard } from "./RegionCard";

const LISTING = {
  connections: [] as unknown[],
  detect: {
    cli: { claude: true, codex: true, gemini: true },
    codex_login: false,
    claude_import: false,
    providers: [
      { id: "openai", label: "OpenAI", wire: "openai", base_url: "https://api.openai.com", local: false },
      { id: "custom", label: "Custom", wire: "openai", base_url: "", local: false },
    ],
  },
  store_available: true,
};

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

let pingBody: { ok: boolean; status: string; code: string };

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  pingBody = { ok: false, status: "error", code: "auth" };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/claude")) return reply({ state: "connected", plan: null, login: { url: null, error: null } });
      if (url.endsWith("/api/engines") && method === "GET") return reply(LISTING);
      if (url.endsWith("/api/engines/api")) return reply({ id: 7, engine: "api", label: "OpenAI", status: "unknown", is_default: false });
      if (url.endsWith("/api/engines/7/test")) return reply(pingBody);
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

  it("Settings > Engines with no connection shows the banner and a way to connect", async () => {
    renderWithApp(<EnginesSettings />);
    await waitFor(() => expect(screen.getByText(t("eng.none_banner"))).toBeInTheDocument());
    expect(screen.getByRole("button", { name: t("eng.connectAnother") })).toBeInTheDocument();
  });
});
