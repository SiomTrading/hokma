import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { BUILTIN_SCREENS, SIDEBAR_SCREENS } from "@/shell/screens";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { StrategiesScreen } from "./StrategiesScreen";
import { locators, ruleLines } from "./rules";

const ROWS = [
  { bank_id: 1, slug: "ema_cross", title: "EMA cross morning", line: "DT", status: "draft", version: 2, source: { kind: "text", reference: "idea of the owner" }, needs_new_rule: false,
    strategy_id: "u_ema_v2", spec_version: 1, updated_at: "2026-11-26", origin: { kind: "hokma" }, agent_reviewed: true, owner_reviewed: false },
  { bank_id: 2, slug: "inside_bar", title: "Inside bar breakout", line: "DT", status: "idea", version: 1, source: { kind: "video", reference: "https://youtu.be/x" }, needs_new_rule: true,
    strategy_id: null, spec_version: null, updated_at: "2026-11-25", origin: { kind: "desk", desk_id: 3, desk_name: "NQ desk" }, agent_reviewed: false, owner_reviewed: false },
];
const DETAIL = {
  bank_id: 1, slug: "ema_cross", title: "EMA cross morning", line: "DT", status: "draft", close_reason: null,
  version: { version: 2, version_id: 9, strategy_id: "u_ema_v2", origin: { kind: "hokma" }, agent_reviewed: true, owner_reviewed: false,
    extraction: { schema: 1, summary: "x", signal_logic: { rule: { value: "fast average crosses above the slow one", origin: "source", cite: [11] }, map: { alpha: "ema_cross" } },
                  sizing: { method: { value: "fixed risk 0.5 percent", origin: "proposed", why: "w" } } } },
  source: { source_id: 4, kind: "text", reference: "idea of the owner", url: null, rights: "own", cited: [{ passage_id: 11, locator: "paragraph 1" }] },
  versions: [{ version: 1, version_id: 8, strategy_id: "u_ema_v1", spec_version: 1, needs_new_rule: false }, { version: 2, version_id: 9, strategy_id: "u_ema_v2", spec_version: 1, needs_new_rule: false }],
  events: [],
};
const CODE_ROW = { rule_version_id: 5, rule_id: 2, bank_id: 1, slug: "ema_cross", title: "EMA cross morning", version: 2, registry_id: "u_ema_v2", stage: "generated", origin: { kind: "hokma" }, summary: null, tests: 4, reviewer: "strategy-reviewer" };

let calls: Array<{ url: string; method: string; body: unknown }> = [];
const respond = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: { method?: string; body?: string }) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body) : undefined });
      if (url === "/api/strategy-bank") return respond({ rows: ROWS, total: 2 });
      if (url.startsWith("/api/strategy-bank/1")) return respond(DETAIL);
      if (url.startsWith("/api/strategy-code?bank_id=1")) return respond({ rows: [CODE_ROW] });
      if (url === "/api/strategy-code/5") return respond({ registry_id: "u_ema_v2", code: "class EmaCross:\n    pass", stage: "generated", agent_reviewed: true, owner_reviewed: false });
      if (url === "/api/strategy-code/5/approve") return respond({ status: "owner_reviewed" });
      return respond({}, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("the Strategies screen", () => {
  it("has its entry in the side menu with its texts in both languages", () => {
    expect(SIDEBAR_SCREENS).toContain("strategies");
    expect(BUILTIN_SCREENS.strategies).toMatchObject({ titleKey: "screen.strategies", navKey: "nav.strategies" });
    expect(t("nav.strategies")).toBe("Strategies");
    const english = t("strategies.optimize");
    setLocale("es");
    expect(t("nav.strategies")).toBe("Estrategias");
    expect(t("strategies.optimize")).toBe("Optimizar");
    expect(t("strategies.optimize")).not.toBe(english);
  });

  it("lists the bank with name, origin, state, version and source, and the search narrows it", async () => {
    renderWithApp(<StrategiesScreen />);
    const rows = await screen.findAllByTestId("strategy-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toContain("EMA cross morning");
    expect(rows[0]!.textContent).toContain("Hokma");
    expect(rows[0]!.textContent).toContain("Draft");
    expect(rows[0]!.textContent).toContain("v2");
    expect(rows[1]!.textContent).toContain("NQ desk");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search strategies" }), "youtu");
    expect(screen.getAllByTestId("strategy-row")).toHaveLength(1);
    expect(screen.getByTestId("strategy-row").textContent).toContain("Inside bar breakout");
  });

  it("opens the detail with the rules and their citation, the versions and the code read only", async () => {
    renderWithApp(<StrategiesScreen />);
    await userEvent.click((await screen.findAllByTestId("strategy-row"))[0]!);
    const rules = await screen.findByTestId("strategy-rules");
    expect(rules.textContent).toContain("fast average crosses above the slow one");
    expect(rules.textContent).toContain("Citation: paragraph 1");
    expect(rules.textContent).toContain("Proposed by Hokma");
    expect(await screen.findByTestId("strategy-code")).toBeTruthy();
    expect(screen.getByTestId("strategy-code").textContent).toContain("class EmaCross");
    await userEvent.click(screen.getByRole("button", { name: "v1" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/strategy-bank/1?version=1")).toBe(true));
  });

  it("approves only after the owner confirms, with the endpoint of the owner", async () => {
    renderWithApp(<StrategiesScreen />);
    await userEvent.click((await screen.findAllByTestId("strategy-row"))[0]!);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Yes, approve" }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/strategy-code/5/approve")?.body).toEqual({ confirm: true }));
  });

  it("Optimize copies the request for the chat with the bank id", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderWithApp(<StrategiesScreen />);
    await userEvent.click((await screen.findAllByTestId("strategy-row"))[0]!);
    await userEvent.click(await screen.findByRole("button", { name: "Optimize" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0]![0]).toContain("backtest_optimize");
    expect(writeText.mock.calls[0]![0]).toContain("bank_id 1");
  });
});

describe("the rules of a sheet", () => {
  it("reads the leaves with their origin and citation and skips the maps", () => {
    const lines = ruleLines(DETAIL.version.extraction);
    expect(lines.map((l) => l.path)).toEqual(["signal_logic / rule", "sizing / method"]);
    expect(lines[0]!.cite).toEqual([11]);
    expect(locators([11, 99], DETAIL.source.cited)).toEqual(["paragraph 1", "#99"]);
  });
});
