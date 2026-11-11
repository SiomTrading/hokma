import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { ExtensionsSection } from "./ExtensionsSection";
import { StatusSection } from "./StatusSection";
import { failure, mockApi } from "./testkit";
import type { Call } from "./testkit";
import type { ExtensionsData, MarketData, StatusData } from "./types";

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

const PLUGIN = { id: "tool@acme", name: "tool", marketplace: "acme", kind: "plugin" as const, enabled: false, summary: 'Plugin "tool@acme" will be activated', state: "disabled" as const, reason: "" };

function extensionsData(over: Partial<ExtensionsData> = {}): ExtensionsData {
  return {
    plugins: [PLUGIN],
    servers: [{ id: "mcp:files", name: "files", kind: "mcp", enabled: true, target: "/usr/bin/files" }],
    marketplaces: [{ name: "acme", source: "github:acme/plugins", repo: "acme/plugins", plugins: 1 }],
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
    tabPlugins: T("ext.tab.plugins", l),
    tabMcp: T("ext.tab.mcp", l),
    install: T("ext.install", l),
    uninstall: T("ext.uninstall", l),
    search: T("ext.market.search", l),
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
    expect(screen.queryByText("/usr/bin/files")).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: words.toggle })).toHaveAttribute("aria-checked", "false");
    await userEvent.click(screen.getByRole("tab", { name: words.tabMcp }));
    expect(screen.getByText("/usr/bin/files")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: words.toggle })).not.toBeInTheDocument();
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
    await userEvent.click(await screen.findByRole("tab", { name: words.tabMcp }));
    expect(await screen.findByText("MCP server runs outside the sandbox")).toBeInTheDocument();
    expect(screen.getByText("It runs a relative path")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: words.approve }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/extensions/requests/ab12cd34/approve")).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: words.reject }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/extensions/requests/ab12cd34/reject")).toBe(true));
  });

  it("opens on Plugins with the marketplace and its search already there, and the search filters", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "GET /api/extensions/marketplace": (call: Call) => {
        const q = new URL(call.path, "http://localhost").searchParams.get("q") ?? "";
        const items = MARKET.items.filter((i) => `${i.display_name} ${i.description}`.toLowerCase().includes(q.toLowerCase()));
        return { ...MARKET, total: items.length, items };
      },
    });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByText("Reads charts")).toBeInTheDocument();
    expect(screen.getByText("Writes reports")).toBeInTheDocument();
    expect(screen.getByText("development")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox", { name: words.search }), "report");
    await waitFor(() => expect(screen.queryByText("Reads charts")).not.toBeInTheDocument());
    expect(screen.getByText("Writes reports")).toBeInTheDocument();
    expect(calls.some((c) => c.path.includes("q=report"))).toBe(true);
  });

  it("installs in one click: the marketplace joins the list, the owner confirms once and the plugin ends on", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "GET /api/extensions/marketplace": MARKET,
      "POST /api/extensions/marketplace": { name: "claude-plugins-official", source: "github:anthropics/claude-plugins-official", warnings: [] },
      "POST /api/extensions/toggle": { id: "alpha@claude-plugins-official", enabled: true, warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    expect(screen.queryByRole("switch", { name: /Alpha/ })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: T("ext.install.of", locale, { name: "Alpha" }) }));
    const dialog = await screen.findByRole("dialog", { name: T("ext.confirm.install", locale, { name: "Alpha" }) });
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: words.confirm }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/toggle")?.body).toEqual({ id: "alpha@claude-plugins-official", enabled: true, confirmed: true }));
    const posts = calls.filter((c) => c.method === "POST").map((c) => c.path);
    expect(posts).toEqual(["/api/extensions/marketplace", "/api/extensions/toggle"]);
  });

  it("an installed marketplace plugin shows the switch and Uninstall instead of Install, and uninstalling asks first", async () => {
    setLocale(locale);
    const installed = { ...MARKET, added: true, items: [{ ...MARKET.items[0]!, installed: true, enabled: true }, MARKET.items[1]!] };
    const calls = mockApi({
      "GET /api/extensions": extensionsData({ plugins: [] }),
      "GET /api/extensions/marketplace": installed,
      "POST /api/extensions/uninstall": { id: "alpha@claude-plugins-official", uninstalled: true, warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByRole("switch", { name: /Alpha/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("button", { name: T("ext.install.of", locale, { name: "Alpha" }) })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: T("ext.install.of", locale, { name: "Beta" }) })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("ext.uninstall.of", locale, { name: "Alpha" }) }));
    const dialog = await screen.findByRole("dialog", { name: T("ext.confirm.uninstall", locale, { name: "Alpha" }) });
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: words.confirm }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/uninstall")?.body).toEqual({ id: "alpha@claude-plugins-official", confirmed: true }));
  });

  it("splits Extensions in Plugins and MCP and remembers the tab in this browser", async () => {
    setLocale(locale);
    mockApi({ "GET /api/extensions": extensionsData(), "GET /api/extensions/marketplace": MARKET, "GET /api/extensions/servers": { servers: [] } });
    const first = renderWithApp(<ExtensionsSection />);
    expect(await screen.findByRole("tab", { name: words.tabPlugins })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: words.tabMcp }));
    expect(screen.getByRole("tab", { name: words.tabMcp })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("searchbox", { name: words.search })).not.toBeInTheDocument();
    first.unmount();
    renderWithApp(<ExtensionsSection />);
    expect(await screen.findByRole("tab", { name: words.tabMcp })).toHaveAttribute("aria-selected", "true");
  });

  it("adds a marketplace by owner/repo and validates the form", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/marketplaces": { name: "plugins", source: "github:acme/plugins", kind: "marketplace", installed: [], plugins: [], warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: words.add }));
    const submit = screen.getAllByRole("button", { name: /^(Agregar|Add)$/ })[0]!;
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText("owner/repo"), "acme/plugins");
    await userEvent.click(submit);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/marketplaces")?.body).toEqual({ repo: "acme/plugins" }));
  });

  it("accepts a GitHub link, normalizes it, and explains a format it cannot use", async () => {
    setLocale(locale);
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/marketplaces": { name: "plugins", source: "github:acme/plugins", kind: "marketplace", installed: [], plugins: [], warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: words.add }));
    const submit = screen.getAllByRole("button", { name: /^(Agregar|Add)$/ })[0]!;
    await userEvent.type(screen.getByPlaceholderText("owner/repo"), "https://gitlab.com/acme/plugins");
    expect(submit).toBeDisabled();
    expect(screen.getByText(T("ext.link.badFormat", locale))).toBeInTheDocument();
    await userEvent.clear(screen.getByPlaceholderText("owner/repo"));
    await userEvent.type(screen.getByPlaceholderText("owner/repo"), "https://github.com/acme/plugins.git/");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/marketplaces")?.body).toEqual({ repo: "acme/plugins" }));
  });
});

describe("Extensions plugins (task 3156)", () => {
  const states = [
    { ...PLUGIN, id: "a@m", name: "alpha", marketplace: "m", enabled: true, state: "active" as const },
    { ...PLUGIN, id: "b@m", name: "beta", marketplace: "m", enabled: false, state: "disabled" as const },
    { ...PLUGIN, id: "c@m", name: "gamma", marketplace: "m", enabled: true, state: "error" as const, reason: "Plugin files are gone" },
  ];

  it("shows Installed first, each plugin with its real state and the reason of an error", async () => {
    setLocale("en");
    mockApi({ "GET /api/extensions": extensionsData({ plugins: states }) });
    renderWithApp(<ExtensionsSection />);
    await screen.findByText("alpha");
    expect(screen.getByLabelText("Status of alpha")).toHaveTextContent("Active");
    expect(screen.getByLabelText("Status of beta")).toHaveTextContent("Off");
    expect(screen.getByLabelText("Status of gamma")).toHaveTextContent("Error");
    expect(screen.getByText("Plugin files are gone")).toBeInTheDocument();
    const installed = screen.getByText("Installed");
    const sources = screen.getByText("Marketplaces you added");
    expect(installed.compareDocumentPosition(sources) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("a card in Error shows the real reason and Retry installs again; other states never read Active (task 3161)", async () => {
    setLocale("en");
    const cards = [
      { ...PLUGIN, id: "c@m", name: "gamma", marketplace: "m", enabled: true, state: "error" as const, reason: "git clone failed: no network", retry: true },
      { ...PLUGIN, id: "d@m", name: "delta", marketplace: "m", enabled: true, state: "claude_only" as const, reason: "This plugin works with Claude Code." },
      { ...PLUGIN, id: "e@m", name: "mem", marketplace: "m", enabled: true, state: "attention" as const, reason: "its worker does not answer on port 37777" },
    ];
    const calls = mockApi({ "GET /api/extensions": extensionsData({ plugins: cards }), "POST /api/extensions/retry": { id: "c@m", installed: true } });
    renderWithApp(<ExtensionsSection />);
    await screen.findByText("gamma");
    expect(screen.getByText("git clone failed: no network")).toBeInTheDocument();
    expect(screen.getByLabelText("Status of delta")).toHaveTextContent("Claude Code only");
    expect(screen.getByLabelText("Status of mem")).toHaveTextContent("Check");
    expect(screen.getAllByRole("button", { name: /^Retry the installation of/ })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Retry the installation of gamma" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/retry")?.body).toEqual({ id: "c@m" }));
  });

  it("lists the plugins of a marketplace repo with several and installs one", async () => {
    setLocale("en");
    const offered = { name: "acme", plugins: [
      { id: "one@acme", name: "one", display_name: "one", description: "First", category: "", installed: false, enabled: false },
      { id: "two@acme", name: "two", display_name: "two", description: "Second", category: "", installed: true, enabled: true },
    ] };
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "GET /api/extensions/marketplaces/acme/plugins": offered,
      "POST /api/extensions/toggle": { id: "one@acme", enabled: true, warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Plugins of acme" }));
    expect(await screen.findByText("First")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Install one" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/toggle")?.body).toEqual({ id: "one@acme", enabled: true, confirmed: true }));
    expect(calls.some((c) => c.path === "/api/extensions/marketplace" && c.method === "POST")).toBe(false);
  });

  it("in a marketplace catalog every plugin has Install or Uninstall, and uninstalling asks first", async () => {
    setLocale("en");
    const offered = { name: "acme", plugins: [
      { id: "one@acme", name: "one", display_name: "one", description: "First", category: "", installed: false, enabled: false },
      { id: "two@acme", name: "two", display_name: "two", description: "Second", category: "", installed: true, enabled: true },
    ] };
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "GET /api/extensions/marketplaces/acme/plugins": offered,
      "POST /api/extensions/uninstall": { id: "two@acme", uninstalled: true, warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Plugins of acme" }));
    expect(await screen.findByText("Second")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install one" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install two" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: T("ext.uninstall.of", "en", { name: "two" }) }));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/extensions/uninstall")?.body).toEqual({ id: "two@acme", confirmed: true }));
  });

  it("opens the plugin list right after adding a repo that offers several", async () => {
    setLocale("en");
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "POST /api/extensions/marketplaces": { name: "acme", source: "github:acme/plugins", kind: "marketplace", installed: [], plugins: [{}, {}], warnings: [] },
      "GET /api/extensions/marketplaces/acme/plugins": { name: "acme", plugins: [{ id: "one@acme", name: "one", display_name: "one", description: "First", category: "", installed: false, enabled: false }] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Add by link" }));
    await userEvent.type(screen.getByPlaceholderText("owner/repo"), "acme/plugins");
    await userEvent.click(screen.getAllByRole("button", { name: "Add" })[0]!);
    expect(await screen.findByText("First")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/extensions/marketplaces/acme/plugins"))).toBe(true);
  });

  it("removes a marketplace after the owner confirms", async () => {
    setLocale("en");
    const calls = mockApi({
      "GET /api/extensions": extensionsData(),
      "DELETE /api/extensions/marketplaces/acme": { name: "acme", removed: true, plugins_removed: [], warnings: [] },
    });
    renderWithApp(<ExtensionsSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Remove the marketplace acme" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/extensions/marketplaces/acme")).toBe(true));
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

describe("Open source notices (T-2)", () => {
  it("next to the version, opens the library, its Apache 2.0 license and its NOTICE text as published", async () => {
    setLocale("en");
    mockApi({ "GET /api/status": STATUS });
    renderWithApp(<StatusSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Open source notices" }));
    const dialog = screen.getByRole("dialog", { name: t("sys.notices.title") });
    const item = within(dialog).getByTestId("notice-lightweight-charts");
    expect(item).toHaveTextContent("lightweight-charts");
    expect(item).toHaveTextContent(t("sys.notices.license", { license: "Apache License 2.0" }));
    expect(within(item).getByRole("link", { name: t("sys.notices.readLicense") })).toHaveAttribute("href", "https://www.apache.org/licenses/LICENSE-2.0");
    const notice = within(item).getByTestId("notice-text").textContent;
    expect(notice).toBe("TradingView Lightweight Charts\u2122\nCopyright (\u0441) 2025 TradingView, Inc. https://www.tradingview.com/\n");
    await userEvent.click(within(dialog).getByRole("button", { name: t("sys.notices.close") }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("has its texts in Spanish too", async () => {
    setLocale("es");
    mockApi({ "GET /api/status": STATUS });
    renderWithApp(<StatusSection />);
    expect(await screen.findByRole("button", { name: "Avisos de c\u00f3digo abierto" })).toBeInTheDocument();
  });
});
