import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import type { Locale } from "@/i18n";
import { FirstRun } from "@/screens/first-run/FirstRun";
import { settingsSectionList } from "@/shell/screens";
import { ShellProvider } from "@/shell/store";
import { useShell } from "@/shell/useShell";
import { installFakeSockets } from "@/test/fakeSocket";
import { renderShell } from "@/test/utils";

/** C5: the keys and the providers are in Connections and nowhere else. */

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Words that only a key field or a provider selector would carry, in both languages. */
function forbiddenTexts(): string[] {
  const keys = [
    "connections.field.key", "connections.field.token", "connections.field.keyId", "connections.field.secret",
    "connections.key.save", "connections.key.remove", "connections.test", "connections.priority.title",
  ];
  const texts = new Set<string>();
  for (const locale of ["en", "es"] as Locale[]) for (const key of keys) texts.add(t(key, undefined, locale));
  for (const name of ["Alpaca", "Finnhub", "Coinbase", "Databento", "Massive"]) texts.add(name);
  return [...texts];
}

function assertNoKeyFields(root: ParentNode, where: string) {
  expect(root.querySelectorAll('input[type="password"]'), where + ": password input").toHaveLength(0);
  expect(root.querySelectorAll("[data-field]"), where + ": key field").toHaveLength(0);
  const text = (root as HTMLElement).textContent ?? "";
  for (const word of forbiddenTexts()) expect(text.includes(word), `${where}: "${word}"`).toBe(false);
  // a provider selector would be a select, a combobox or a listbox with a provider among its options
  for (const select of root.querySelectorAll("select, [role=combobox], [role=listbox]")) {
    expect((select as HTMLElement).textContent ?? "", where + ": selector").not.toMatch(/alpaca|finnhub|coinbase|databento|massive/i);
  }
}

beforeEach(() => {
  setLocale("en");
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(async () => reply({ detail: { code: "not_found", message: "nothing here" } }, 404)));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("C5: one place", () => {
  it("every section of Settings, and the chat, have no key field and no provider selector", async () => {
    renderShell();
    const user = userEvent.setup();
    assertNoKeyFields(document.body, "shell at start");
    await user.click(screen.getByRole("button", { name: t("nav.settings") }));
    const dialog = await screen.findByRole("dialog", { name: t("settings.title") });
    const sections = settingsSectionList();
    expect(sections.length).toBeGreaterThanOrEqual(6);
    for (const section of sections) {
      await user.click(within(dialog).getByRole("button", { name: t(section.titleKey) }));
      await waitFor(() => expect(within(dialog).getByRole("heading", { name: t(section.titleKey) })).toBeInTheDocument());
      assertNoKeyFields(dialog, "settings " + section.id);
    }
  });

  it("the first run, step by step, has no key field and no provider selector", async () => {
    const state = {
      completed: false,
      profile: null,
      owner: { owner_name: "", fund_name: "" },
      workdir: { path: "/home/ana/Hokma", exists: false, git: false, manual: false, options: [{ id: "documents", provider: null, path: "/home/ana/Documents/Hokma" }] },
      database: { engine: "sqlite", location: null },
      agents: { total: 0 },
      claude: { state: "unknown", plan: null },
      version: "0.0.6",
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      const route = `${init?.method ?? "GET"} ${url.pathname}`;
      switch (route) {
        case "GET /api/setup": return reply(state);
        case "GET /api/claude": return reply({ state: "connected", installed: true, plan: "Max", version: "9", login: { running: false, url: null } });
        case "POST /api/workdir": return reply({ path: "/home/ana/Documents/Hokma", created: true, git_initialized: true, git: true });
        case "GET /api/owner/preview": return reply({ text: "# Hokma" });
        case "POST /api/owner": return reply(state);
        case "GET /api/protections": return reply({ always_on: true, rules: [] });
        case "GET /api/profile/permission-mode": return reply({ mode: "ask", intro_seen: false });
        default: return reply({ detail: "unexpected " + route }, 404);
      }
    }));
    setToken("test-session-token-0123456789");
    const user = userEvent.setup();
    function Where() {
      const shell = useShell();
      return <span data-testid="where">{shell.activeKey}</span>;
    }
    render(
      <ShellProvider>
        <div className="hk">
          <FirstRun pollMs={20} />
          <Where />
        </div>
      </ShellProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: t("firstrun.dialog") });
    const next = () => screen.getByRole("button", { name: t("firstrun.next") });

    await screen.findByText(t("firstrun.claude.connectedPlan", { plan: "Max" }));
    assertNoKeyFields(dialog, "first run: Claude");
    await user.click(next());
    await screen.findByRole("heading", { name: t("firstrun.folder.title") });
    assertNoKeyFields(dialog, "first run: folder");
    await user.click(next());
    await screen.findByRole("heading", { name: t("firstrun.db.title") });
    assertNoKeyFields(dialog, "first run: database");
    await user.click(next());
    await screen.findByRole("heading", { name: t("firstrun.name.title") });
    assertNoKeyFields(dialog, "first run: name");
    await user.type(screen.getByLabelText(t("firstrun.name.owner")), "Ana Torres");
    await user.click(next());
    await screen.findByTestId("firstrun-mode");
    assertNoKeyFields(dialog, "first run: done");
  });
});
