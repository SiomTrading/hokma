import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { ExtensionsSection } from "./ExtensionsSection";
import { NewsSection } from "./NewsSection";
import { StatusSection } from "./StatusSection";
import { failure, mockApi } from "./testkit";
import type { ExtensionsData, MarketData, NewsData, StatusData } from "./types";

afterEach(() => vi.unstubAllGlobals());

const PLUGIN = { id: "tool@acme", name: "tool", marketplace: "acme", kind: "plugin" as const, enabled: false, summary: 'Plugin "tool@acme" will be activated' };

function extensionsData(over: Partial<ExtensionsData> = {}): ExtensionsData {
  return {
    plugins: [PLUGIN],
    servers: [{ id: "mcp:files", name: "files", kind: "mcp", enabled: true, target: "/usr/bin/files" }],
    marketplaces: [{ name: "acme", source: "github:acme/plugins" }],
    requests: [],
    problems: [],
    database: true,
    ...over,
  };
}

const MARKET: MarketData = {
  marketplace: "claude-plugins-official",
  repo: "anthropics/claude-plugins-official",
  added: false,
  total: 2,
  items: [
    { id: "alpha@claude-plugins-official", name: "alpha", display_name: "Alpha", description: "Reads charts", category: "development", installed: false, enabled: false },
    { id: "beta@claude-plugins-official", name: "beta", display_name: "Beta", description: "Writes reports", category: "", installed: false, enabled: false },
  ],
};

type Loc = "es" | "en";
const T = (key: string, l: Loc, params?: Record<string, string | number>) => t(key, params, l);
function extWords(l: Loc) {
  return {
    toggle: T("ext.toggle", l, { name: "tool" }),
    confirm: T("ext.confirm.ok", l),
    market: T("ext.market.title", l),
    add: T("ext.add", l),
    approve: T("ext.requests.approve", l),
    reject: T("ext.requests.reject", l),
    activate: T("ext.confirm.enable", l, { name: "tool" }),
  };
}

describe.each([
  ["es", extWords("es")],
  ["en", extWords("en")],
] as const)("Extensions (%s)", (locale, words) => {
  it("lists what is installed, with the real marketplace and the MCP server", async () => {
    setLocale(locale);
    mockApi({ "GET /api/extensions": extensionsData() });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByText("tool")).toBeInTheDocument();
    expect(screen.getAllByText("acme").length).toBeGreaterThan(0);
    expect(screen.getByText("/usr/bin/files")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: words.toggle })).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByText(/Cat.logo SiomTrading/)).not.toBeInTheDocument();
  });

  it("turns a plugin on only after the confirmation, and shows what it activates", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/toggle": { id: "tool@acme", enabled: true, warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("switch", { name: words.toggle }));
    const dialog = await screen.findByRole("dialog", { name: words.activate });
    expect(within(dialog).getByText(/will be activated/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);

    await userEvent.click(within(dialog).getByRole("button", { name: words.confirm }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/toggle")?.body).toEqual({ id: "tool@acme", enabled: true, confirmed: true }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: words.activate })).not.toBeInTheDocument());
  });

  it("approves and rejects a request from an agent", async () => {
    setLocale(locale);
    const request = { id: "ab12cd34", origin: "inbox" as const, kind: "mcp" as const, name: "srv", requested_at: "2026-11-05 10:00", reason: null, risks: ["It runs a relative path"], summary: "MCP server runs outside the sandbox" };
    const calls = mockApi({
      "GET /api/extensions": extensionsData({ requests: [request] }),
      "POST /api/extensions/requests/ab12cd34/approve": { id: "ab12cd34", status: "applied" },
      "POST /api/extensions/requests/ab12cd34/reject": { id: "ab12cd34", status: "rejected" },
    });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByText("MCP server runs outside the sandbox")).toBeInTheDocument();
    expect(screen.getByText("It runs a relative path")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: words.approve }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/extensions/requests/ab12cd34/approve")).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: words.reject }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/extensions/requests/ab12cd34/reject")).toBe(true));
  });

  it("lists the Anthropic marketplace for real and adds it", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "GET /api/extensions/marketplace": MARKET,
      "POST /api/extensions/marketplace": { name: "claude-plugins-official", source: "github:anthropics/claude-plugins-official", warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: words.market }));
    expect(await screen.findByText("Reads charts")).toBeInTheDocument();
    expect(screen.getByText("Writes reports")).toBeInTheDocument();
    expect(screen.getByText("development")).toBeInTheDocument();
    const turnOnAlpha = screen.getByRole("switch", { name: /Alpha/ });
    expect(turnOnAlpha).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /Agregar a mi lista|Add to my list/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/extensions/marketplace")).toBe(true));
  });

  it("adds a marketplace by owner/repo and validates the form", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/marketplaces": { name: "plugins", source: "github:acme/plugins", warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: words.add }));
    const submit = screen.getAllByRole("button", { name: /^(Agregar|Add)$/ })[0]!;
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText("owner/repo"), "acme/plugins");
    await userEvent.click(submit);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/marketplaces")?.body).toEqual({ repo: "acme/plugins" }));
  });
});

describe("Extensions states", () => {
  it("shows an honest empty state, with no sample entries and no catalog button", async () => {
    mockApi({ "GET /api/extensions": extensionsData({ plugins: [], servers: [], marketplaces: [] }) });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByText(T("ext.empty.title", "es"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cat.logo/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/pr.ximamente|coming soon/i)).not.toBeInTheDocument();
  });

  it("shows the error with a retry and the server message on a failed change", async () => {
    mockApi({ "GET /api/extensions": failure(500, "Boom") });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByText("Boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
  });

  it("keeps the dialog open and shows the message when the change fails", async () => {
    mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/toggle": failure(503, "Database not ready"),
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("switch", { name: "Cambiar tool" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Confirmar" }));
    expect(await within(dialog).findByText("Database not ready")).toBeInTheDocument();
  });

  it("disables the switches while the database is not ready", async () => {
    mockApi({ "GET /api/extensions": extensionsData({ database: false }) });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByRole("switch", { name: "Cambiar tool" })).toBeDisabled();
    expect(screen.getByText(/base de datos todav/i)).toBeInTheDocument();
  });
});

const NEWS: NewsData = {
  installed: { version: "0.0.5", installed_at: "2026-11-04T12:00:00Z" },
  changelog: [{ version: "0.0.5", body: "- Chat with photos\n- A <script>alert(1)</script> line" }],
  latest: { version: "0.0.6", notes: "- Faster charts", published_at: "2026-11-05T10:00:00Z" },
  latest_error: null,
  newer: true,
};

function newsWords(l: Loc) {
  return {
    update: T("news.update", l),
    newer: T("news.newer", l, { version: "0.0.6" }),
    coming: T("news.coming", l),
    done: T("news.updated", l, { version: "0.0.6" }).split(".")[0],
  };
}

describe.each([
  ["es", newsWords("es")],
  ["en", newsWords("en")],
] as const)("News (%s)", (locale, words) => {
  it("shows the installed version, the notice, the real notes and the changelog without raw HTML", async () => {
    setLocale(locale);
    mockApi({ "GET /api/news": NEWS });
    renderWithApp(<NewsSection />);
    expect((await screen.findAllByText("v0.0.5", { selector: ".pill" })).length).toBe(2);
    expect(screen.getByText(words.newer)).toBeInTheDocument();
    expect(screen.getByText(words.coming)).toBeInTheDocument();
    expect(screen.getByText("Faster charts")).toBeInTheDocument();
    expect(screen.getByText("Chat with photos")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    expect(screen.queryByText(/sesi.n|cohorte|session \d|cohort/i)).not.toBeInTheDocument();
  });

  it("updates through /api/update and reports the result", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/news": NEWS,
      "POST /api/update": { status: "updated", from_version: "0.0.5", to_version: "0.0.6", restart_required: true },
    });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: words.update }));
    expect(await screen.findByText(new RegExp(words.done))).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/update")).toBe(true);
  });
});

describe("News states", () => {
  it("has no update button and no 'coming' block when there is nothing newer", async () => {
    mockApi({ "GET /api/news": { ...NEWS, latest: { version: "0.0.5", notes: null, published_at: null }, newer: false } });
    renderWithApp(<NewsSection />);
    expect(await screen.findByText(T("news.upToDate", "es"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actualizar Hokma" })).not.toBeInTheDocument();
    expect(screen.queryByText("Lo que viene")).not.toBeInTheDocument();
  });

  it("shows the update error from the server", async () => {
    mockApi({ "GET /api/news": NEWS, "POST /api/update": failure(502, "No access to the release") });
    renderWithApp(<NewsSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Actualizar Hokma" }));
    expect(await screen.findByText("No access to the release")).toBeInTheDocument();
  });

  it("offers to check again when the latest version could not be read", async () => {
    mockApi({ "GET /api/news": { ...NEWS, latest: null, newer: false, latest_error: "Offline" } });
    renderWithApp(<NewsSection />);
    expect(await screen.findByText(/Offline/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Buscar actualizaciones" })).toBeInTheDocument();
  });
});

const STATUS: StatusData = {
  version: "0.0.5",
  workdir: "/home/ana/Hokma",
  checks: [
    { name: "git", status: "ok", detail: "/usr/bin/git" },
    { name: "Claude", status: "warn", detail: "No session. Run claude to log in." },
    { name: "Database", status: "fail", detail: "Cannot be read" },
  ],
  summary: { ok: 1, warn: 1, fail: 1 },
};

function statusWords(l: Loc) {
  return {
    fine: T("sys.state.ok", l),
    attention: T("sys.state.warn", l),
    failing: T("sys.state.fail", l),
    recheck: T("sys.recheck", l),
  };
}

describe.each([
  ["es", statusWords("es")],
  ["en", statusWords("en")],
] as const)("System status (%s)", (locale, words) => {
  it("shows each check with its state and how to fix it, and checks again", async () => {
    setLocale(locale);
    const calls = mockApi({ "GET /api/status": STATUS });
    renderWithApp(<StatusSection />);
    expect(await screen.findByText("/usr/bin/git")).toBeInTheDocument();
    expect(screen.getByText("No session. Run claude to log in.")).toBeInTheDocument();
    expect(screen.getByText(words.fine)).toBeInTheDocument();
    expect(screen.getByText(words.attention)).toBeInTheDocument();
    expect(screen.getByText(words.failing)).toBeInTheDocument();
    expect(screen.getByText(/\/home\/ana\/Hokma/)).toBeInTheDocument();
    const before = calls.length;
    await userEvent.click(screen.getByRole("button", { name: words.recheck }));
    await waitFor(() => expect(calls.length).toBeGreaterThan(before));
  });
});

describe("System status errors", () => {
  it("shows the error with a retry", async () => {
    mockApi({ "GET /api/status": failure(500, "Checks failed") });
    renderWithApp(<StatusSection />);
    expect(await screen.findByText("Checks failed")).toBeInTheDocument();
  });
});
