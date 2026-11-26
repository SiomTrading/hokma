import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setToken } from "@/api";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { RulesTab } from "./tabs/RulesTab";
import type { RulesView } from "./rulesApi";
import type { DeskSummary } from "./types";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const DESK = { id: 3, name: "Opciones" } as unknown as DeskSummary;

const EMPTY: RulesView = {
  desk_id: 3, line: "SWO", legacy: false, fixed: ["contract", "monitor", "premium_selling", "broker_rules"], active: [], proposed: [],
  catalog: [{ id: "size_premium", unit: "usd", lines: ["SWO"] }, { id: "daily_loss", unit: "usd", lines: null }, { id: "events", unit: "minutes", lines: null }],
  suggested: [{ id: "s_size_premium", kind: "control", control_id: "size_premium", value: 500, unit: "usd" }, { id: "s_i1", kind: "instruction" }],
  assets: [], assets_on: false,
};

function server(initial: RulesView) {
  let state = initial;
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: String(url), method, body });
    if (method === "POST" && url === "/api/desks/3/rules") {
      state = { ...state, active: [...state.active, body.kind === "instruction"
        ? { id: "i1", kind: "instruction", text: body.text, source: body.source, on: true }
        : { id: body.control_id, kind: "control", control_id: body.control_id, value: body.value, unit: "usd", source: body.source, on: true }] };
    }
    if (method === "DELETE") state = { ...state, active: [] };
    if (url.endsWith("/approve")) state = { ...state, proposed: [], active: [{ id: "i9", kind: "instruction", text: "Cerrar antes de resultados", source: "chat", on: true }] };
    if (url === "/api/desks/3/rules-dismiss") state = { ...state, suggested: state.suggested.filter((s) => s.id !== body.suggested_id) };
    return json(state);
  }));
  return calls;
}

const open = async () => {
  renderWithApp(<RulesTab desk={DESK} />);
  await screen.findByText(t("desks.rules.active.title"));
};

beforeEach(() => { setLocale("es"); setToken(TEST_TOKEN); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Rules tab (SPEC-025)", () => {
  it("shows an empty desk as allowed, the fixed block, and nothing is active until the owner adds it", async () => {
    server(EMPTY);
    await open();
    expect(screen.getByText(t("desks.rules.empty"))).toBeInTheDocument();
    const fixed = screen.getByRole("region", { name: new RegExp(t("desks.rules.fixed.title")) });
    for (const name of ["contract", "monitor", "premium_selling", "broker_rules"]) expect(fixed).toHaveTextContent(t(`desks.rules.fixed.${name}`));
    const suggested = screen.getByRole("region", { name: t("desks.rules.suggested.title") });
    expect(suggested).toHaveTextContent("500 USD");
    expect(suggested).toHaveTextContent(t("desks.rules.i.s_i1"));
  });

  it("adds a suggested control and a suggested instruction with their origin", async () => {
    const calls = server(EMPTY);
    await open();
    const add = () => screen.getByRole("button", { name: new RegExp(t("desks.rules.suggested.add") + ": " + t("desks.rules.c.size_premium")) });
    await userEvent.click(add());                                                       // the first click asks the mode: nothing is sent
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    expect(add()).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.mode.ask") }), "restriction");
    await userEvent.click(add());
    await screen.findByText(t("desks.rules.source.suggested"));
    expect(calls.at(-1)).toMatchObject({ method: "POST", body: { kind: "control", control_id: "size_premium", value: 500, source: "suggested", mode: "restriction" } });
    const active = screen.getByRole("region", { name: t("desks.rules.active.title") });
    expect(active).toHaveTextContent(t("desks.rules.pill.control"));
    expect(active).toHaveTextContent("500 USD");
  });

  it("adds a control by hand from the list and an instruction in free text", async () => {
    const calls = server(EMPTY);
    await open();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.add.pick") }), "daily_loss");
    await userEvent.type(screen.getByRole("textbox", { name: t("desks.rules.add.value") }), "400");
    expect(screen.getByRole("button", { name: t("desks.rules.add.control") })).toBeDisabled();           // no mode, nothing is saved
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.mode.ask") }), "restriction");
    expect(screen.getByRole("button", { name: t("desks.rules.add.control") })).toBeDisabled();           // a loss limit also asks what happens at the limit
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.trip.ask") }), "block_and_flatten");
    await userEvent.click(screen.getByRole("button", { name: t("desks.rules.add.control") }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && (c.body as { control_id?: string }).control_id === "daily_loss")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ kind: "control", control_id: "daily_loss", value: 400, source: "hand", mode: "restriction", on_trip: "block_and_flatten" });
    await userEvent.type(screen.getByRole("textbox", { name: t("desks.rules.add.text") }), "No operar el viernes");
    await userEvent.click(screen.getByRole("button", { name: t("desks.rules.add.instruction") }));
    const active = screen.getByRole("region", { name: t("desks.rules.active.title") });
    await within(active).findByText("No operar el viernes");
    expect(within(active).getByText(t("desks.rules.pill.instruction"))).toBeInTheDocument();
  });

  it("lists the proposals of the chief and applies one only when the owner approves it", async () => {
    const calls = server({ ...EMPTY, proposed: [{ id: "p1", kind: "instruction", text: "Cerrar antes de resultados" }] });
    await open();
    const block = screen.getByRole("region", { name: t("desks.rules.proposed.title") });
    expect(block).toHaveTextContent("Cerrar antes de resultados");
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    await userEvent.click(within(block).getByRole("button", { name: t("desks.rules.proposed.approve") }));
    await waitFor(() => expect(screen.queryByRole("region", { name: t("desks.rules.proposed.title") })).not.toBeInTheDocument());
    expect(calls.at(-1)).toMatchObject({ method: "POST", url: "/api/desks/3/rules-proposals/p1/approve" });
    expect(screen.getByRole("region", { name: t("desks.rules.active.title") })).toHaveTextContent(t("desks.rules.source.chat"));
  });

  it("shows a control proposal with the saved value and unit, never the free text of the agent", async () => {
    server({ ...EMPTY, proposed: [{ id: "p2", kind: "control", control_id: "daily_loss", value: 400, unit: "usd", loosens: false, current: null, text: "texto libre del agente 99999" }] });
    await open();
    const block = screen.getByRole("region", { name: t("desks.rules.proposed.title") });
    expect(block).toHaveTextContent(t("desks.rules.c.daily_loss"));
    expect(block).toHaveTextContent("400 USD");
    expect(block).not.toHaveTextContent("99999");
  });

  it("a proposal that loosens a control shows from X to Y and sends the confirmation only after a second, different button", async () => {
    const calls = server({ ...EMPTY, proposed: [{ id: "p3", kind: "control", control_id: "daily_loss", value: 900, unit: "usd", loosens: true, current: 400, text: "x" }] });
    await open();
    const block = screen.getByRole("region", { name: t("desks.rules.proposed.title") });
    expect(block).toHaveTextContent(t("desks.rules.proposed.loosens", { from: "400", to: "900", unit: "USD" }));
    await userEvent.click(within(block).getByRole("button", { name: t("desks.rules.proposed.approve") }));
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);                          // the first click sends nothing
    await userEvent.click(within(block).getByRole("button", { name: t("desks.rules.proposed.confirmLoosen") }));
    await waitFor(() => expect(calls.at(-1)).toMatchObject({ method: "POST", url: "/api/desks/3/rules-proposals/p3/approve", body: { confirm_loosen: true } }));
  });

  it("shows the controls a desk that already existed has, the unit the code applies, and a saved value the code does not apply", async () => {
    server({ ...EMPTY, legacy: true, suggested: [], active: [
      { id: "daily_loss", kind: "control", control_id: "daily_loss", value: 900, unit: "usd", source: "existing", on: true },
      { id: "cooldown", kind: "control", control_id: "cooldown", value: 10, unit: "minutes", source: "existing", on: false },
      { id: "assets_only", kind: "control", control_id: "assets_only", value: true, unit: "assets", source: "existing", on: true, readonly: true }] });
    await open();
    expect(screen.getByText(t("desks.rules.legacy"))).toBeInTheDocument();
    const active = screen.getByRole("region", { name: t("desks.rules.active.title") });
    expect(active).toHaveTextContent(t("desks.rules.c.daily_loss"));
    expect(active).toHaveTextContent("900 USD");
    expect(active).toHaveTextContent("10 min");
    expect(active).toHaveTextContent(t("desks.rules.notApplied"));
    expect(active).toHaveTextContent(t("desks.rules.assets.none"));
    expect(active).toHaveTextContent(t("desks.rules.readonly"));
    expect(screen.queryByText(t("desks.rules.empty"))).not.toBeInTheDocument();
  });

  it("a notice asks how long to wait and sends the minutes", async () => {
    const calls = server(EMPTY);
    await open();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.add.pick") }), "size_premium");
    await userEvent.type(screen.getByRole("textbox", { name: t("desks.rules.add.value") }), "300");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.mode.ask") }), "notice");
    const wait = screen.getByRole("textbox", { name: t("desks.rules.mode.wait") });
    await userEvent.clear(wait);
    await userEvent.type(wait, "45");
    await userEvent.click(screen.getByRole("button", { name: t("desks.rules.add.control") }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ control_id: "size_premium", mode: "notice", timeout_min: 45 });
  });

  it("shows the mode of every rule and lets the owner change the mode of the list of assets", async () => {
    const calls = server({ ...EMPTY, suggested: [], active: [
      { id: "assets_only", kind: "control", control_id: "assets_only", value: true, unit: "assets", source: "existing", on: true, readonly: true, mode: "notice", timeout_min: 30 },
      { id: "daily_loss", kind: "control", control_id: "daily_loss", value: 400, unit: "usd", source: "hand", on: true, mode: "restriction", on_trip: "block_and_flatten_all" }] });
    await open();
    const active = screen.getByRole("region", { name: t("desks.rules.active.title") });
    expect(active).toHaveTextContent(t("desks.rules.mode.noticeText", { min: "30" }));
    expect(active).toHaveTextContent(t("desks.rules.trip.block_and_flatten_all"));
    await userEvent.click(within(active).getByRole("button", { name: new RegExp(t("desks.rules.mode.change")) }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: t("desks.rules.mode.ask") }), "restriction");
    await userEvent.click(screen.getByRole("button", { name: t("desks.rules.save") }));
    await waitFor(() => expect(calls.at(-1)).toMatchObject({ method: "PUT", url: "/api/desks/3/rules/assets_only", body: { mode: "restriction" } }));
  });

  it("an account closed for the day shows up and opening it needs a second, explicit click", async () => {
    const calls = server({ ...EMPTY, suggested: [], accounts: [{ account_id: 7, name: "NQ 25k", state: "day_closed", rule_id: "daily_loss", loss: "520" }] });
    await open();
    const block = screen.getByRole("region", { name: t("desks.rules.accounts.title") });
    expect(block).toHaveTextContent("NQ 25k");
    await userEvent.click(within(block).getByRole("button", { name: t("desks.rules.closed.lift") }));
    expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    await userEvent.click(within(block).getByRole("button", { name: t("desks.rules.closed.lift.confirm") }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/accounts/7/day-close/lift" && c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/lift"))?.body).toEqual({ confirm: true });
  });

  it("says so when the rules cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: { code: "x", message: "boom" } }, 500)));
    renderWithApp(<RulesTab desk={DESK} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("has every text in es and en, with no long dash", () => {
    setLocale("en");
    const en = getCatalog("en");
    setLocale("es");
    const es = getCatalog("es");
    const keys = Object.keys(es).filter((k) => k.startsWith("desks.rules."));
    expect(keys.length).toBeGreaterThan(60);
    for (const key of keys) {
      expect(en[key], key).toBeTruthy();
      expect(es[key] + en[key]).not.toContain(String.fromCharCode(0x2014));
    }
  });
});
