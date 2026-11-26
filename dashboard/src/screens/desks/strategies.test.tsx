import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { StrategiesTab } from "./tabs/StrategiesTab";
import type { DeskSummary } from "./types";

const DESK = {
  id: 4, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
} as DeskSummary;

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const PAYOFF = { spots: [80, 90, 100, 110, 120], pnl_usd: [-500, -500, -100, 400, 400] };
const base = {
  family: "bullish", cost_usd: 100, credit_usd: 0, capital_required_usd: 100, max_profit_usd: 400, max_loss_usd: 500, unbounded_profit: false, unbounded_loss: false,
  breakevens: [101.2], p_profit: 0.41, expected_value_usd: 12.5, payoff: PAYOFF, status_message: null, locked: null, can_open_detail: true,
  legs: [{ position: "long", kind: "call", strike: 100, premium: 1 }, { position: "short", kind: "call", strike: 110, premium: 0.2 }],
};
const GOOD = { ...base, id: "debit_call_spread", structure: "debit_call_spread", net_premium_usd: 100, status: "ok",
  verdict: { verdict: "recommended", label: "Recomendada", reason: "Probabilidad 41% con riesgo de 500.", backed_by: { name: "Caleb", seat: "A07" } } };
const LOCKED = { ...base, id: "iron_condor", structure: "iron_condor", net_premium_usd: -150, status: "locked", locked: "premium_selling", can_open_detail: false,
  status_message: "La venta de prima esta cerrada en esta mesa para AAPL.", verdict: { verdict: "closed", label: "Cerrada", reason: "La venta de prima esta cerrada en esta mesa para AAPL.", backed_by: { source: "premium_switch" } } };
const OVER = { ...base, id: "long_call", structure: "long_call", net_premium_usd: 1250, capital_required_usd: 1250, status: "over_budget",
  status_message: "No cabe en tu presupuesto: necesita 1,250.00 USD y tu presupuesto es 300.00 USD.",
  verdict: { verdict: "over_budget", label: "Fuera de presupuesto", reason: "No cabe en tu presupuesto: necesita 1,250.00 USD y tu presupuesto es 300.00 USD.", backed_by: { source: "budget" } } };
const NONE = { ...base, id: "long_put", structure: "long_put", net_premium_usd: 80, status: "ok",
  verdict: { verdict: null, label: null, reason: "Sin veredicto: no model answered.", backed_by: null, code: "engine" } };

const RESULT = {
  ok: true, symbol: "AAPL", view: "bullish", expiration: "2026-12-18", days: 23, spot: 100, source: "Schwab", as_of: "2026-11-25T15:00:00Z", delayed: false, notes: [],
  vol_view: "flat", vol_view_source: "default_flat_uncalibrated", premium_selling: { open: false, reason: "cerrada" }, reading: "Lectura de la mesa: el call spread destaca.",
  model: { used: true, agent: { name: "Caleb" }, reason: null }, cards: [GOOD, LOCKED, OVER, NONE], skipped: [],
};

interface Stub { last?: unknown; ask?: () => Response; calls: Array<{ url: string; method: string; body: unknown }> }

function stubApi(stub: Stub) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    stub.calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (String(url).endsWith("/strategies/last")) return json(stub.last ?? { form: null, result: null });
    if (String(url).endsWith("/strategies") && method === "POST") return stub.ask ? stub.ask() : json(RESULT);
    if (String(url).includes("/options/expirations")) return json({ symbol: "AAPL", source: "Schwab", expirations: [{ date: "2026-12-18", days: 23 }, { date: "2027-01-15", days: 51 }] });
    return json({ id: 4, name: "Mesa A", assets: [{ symbol: "AAPL", note: null }, { symbol: "NVDA", note: null }], connections: [] });
  }));
}

const card = (name: string) => screen.getByRole("article", { name }) as HTMLElement;
const asked = (stub: Stub) => stub.calls.filter((c) => c.method === "POST" && c.url.endsWith("/strategies"));

beforeEach(() => {
  setLocale("es");
  clearToken();
  setToken(TEST_TOKEN);
  localStorage.clear();
  installFakeSockets();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Strategies panel", () => {
  it("sends the form as the contract says and shows one card per strategy with its chart", async () => {
    const stub: Stub = { calls: [] };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const asset = await screen.findByLabelText(t("desks.strategies.asset"));
    expect(asset).toHaveValue("AAPL");                                                        // the first asset of the desk
    await userEvent.selectOptions(screen.getByLabelText(t("desks.strategies.view")), "bullish");
    await userEvent.type(screen.getByLabelText(t("desks.strategies.target")), "120");
    await userEvent.type(screen.getByLabelText(t("desks.strategies.budget")), "3000");
    await waitFor(() => expect(screen.getByRole("option", { name: /2026-12-18/ })).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText(t("desks.strategies.expiration")), "2026-12-18");
    await userEvent.selectOptions(screen.getByLabelText(t("desks.strategies.priority")), "max_probability");
    await userEvent.click(screen.getByRole("button", { name: t("desks.strategies.go") }));
    await screen.findByRole("article", { name: t("desks.strategies.s.debit_call_spread") });
    expect(asked(stub)).toHaveLength(1);
    expect(asked(stub)[0]!.body).toEqual({ symbol: "AAPL", view: "bullish", target: 120, budget: 3000, expiration: "2026-12-18", priority: "max_probability" });
    const good = card(t("desks.strategies.s.debit_call_spread"));
    expect(good.querySelector("svg.strat-chart")).not.toBeNull();
    expect(within(good).getByText(t("desks.strategies.verdict.recommended"))).toBeInTheDocument();
    expect(within(good).getByText(/Caleb/)).toBeInTheDocument();                              // who backs it
    expect(within(good).getByText("41.0%")).toBeInTheDocument();
    expect(within(good).getByText("101.20")).toBeInTheDocument();
    expect(screen.getByText(/el call spread destaca/)).toBeInTheDocument();
  });

  it("a card of premium selling shows its lock and its reason, never hidden", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "neutral", target: null, budget: 3000, expiration: null, priority: "balanced" }, result: RESULT } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const locked = await screen.findByRole("article", { name: t("desks.strategies.s.iron_condor") });
    expect(within(locked).getByText(t("desks.strategies.locked"))).toBeInTheDocument();
    expect(within(locked).getAllByText(/La venta de prima esta cerrada/).length).toBeGreaterThan(0);
    expect(within(locked).getByText(t("desks.strategies.verdict.closed"))).toBeInTheDocument();
    expect(within(locked).queryByRole("button", { name: t("desks.strategies.openDetail") })).toBeNull();   // a closed card has no "Abrir detalle"
    expect(locked.querySelector("svg.strat-chart")).not.toBeNull();                             // and its numbers stay
  });

  it("a card that does not fit says so with its cost", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "bullish", target: null, budget: 300, expiration: null, priority: "balanced" }, result: RESULT } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const over = await screen.findByRole("article", { name: t("desks.strategies.s.long_call") });
    expect(within(over).getAllByText(t("desks.strategies.verdict.over_budget")).length).toBeGreaterThan(0);
    expect(within(over).getByText(/necesita 1,250.00 USD/)).toBeInTheDocument();
    expect(within(over).getByText(/1250,00/)).toBeInTheDocument();                              // the cost in the table
  });

  it("without a verdict the card says why and its numbers stay; without expected value it says no data", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "bullish", target: null, budget: null, expiration: null, priority: "balanced" }, result: { ...RESULT, cards: [{ ...NONE, expected_value_usd: null, p_profit: null }] } } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const none = await screen.findByRole("article", { name: t("desks.strategies.s.long_put") });
    expect(within(none).getByText(/Sin veredicto: no model answered/)).toBeInTheDocument();
    expect(within(none).getByText(t("desks.strategies.noVerdictShort"))).toBeInTheDocument();
    expect(within(none).getAllByText(t("desks.strategies.noDatum")).length).toBe(2);             // probability and expected value: "Sin dato", never 0
    expect(within(none).getByText(/80,00/)).toBeInTheDocument();
  });

  it("the default volatility is shown as uncalibrated and the calibrated one is not", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "bullish", target: null, budget: null, expiration: null, priority: "balanced" }, result: RESULT } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    expect(await screen.findByText(t("desks.strategies.volUncalibrated"))).toBeInTheDocument();
    cleanup();
    stubApi({ calls: [], last: { form: stub.last && (stub.last as { form: unknown }).form, result: { ...RESULT, vol_view_source: "vol_analysis" } } });
    renderWithApp(<StrategiesTab desk={DESK} />);
    await screen.findByRole("article", { name: t("desks.strategies.s.debit_call_spread") });
    expect(screen.queryByText(t("desks.strategies.volUncalibrated"))).toBeNull();
  });

  it("Propose is off for a card that does not fit the budget, with its reason, and nothing is sent", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "bullish", target: null, budget: 300, expiration: null, priority: "balanced" }, result: RESULT } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const over = await screen.findByRole("article", { name: t("desks.strategies.s.long_call") });
    const propose = within(over).getByRole("button", { name: t("desks.detail.propose") });
    expect(propose).toBeDisabled();
    expect(propose).toHaveAttribute("title", OVER.status_message);
    expect(stub.calls.some((c) => c.url.includes("/orders") || c.url.includes("/exec") || c.url.includes("/option-detail"))).toBe(false);
  });

  it("an invalid request (422) shows the message of the server and keeps the form", async () => {
    const stub: Stub = { calls: [], ask: () => json({ detail: { code: "bad_strategy_args", message: "La consulta de estrategias no es valida: revisa el activo." } }, 422) };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    await screen.findByLabelText(t("desks.strategies.asset"));
    await userEvent.click(screen.getByRole("button", { name: t("desks.strategies.go") }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no es valida/);
    expect(screen.getByLabelText(t("desks.strategies.asset"))).toHaveValue("AAPL");
    expect(screen.queryByRole("article")).toBeNull();
  });

  it("a symbol that is not one is refused in the form, before asking", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "AAPL", view: "bullish", target: null, budget: null, expiration: null, priority: "balanced" }, result: null } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    const asset = await screen.findByLabelText(t("desks.strategies.asset"));
    await userEvent.clear(asset);
    await userEvent.type(asset, "12");
    await userEvent.click(screen.getByRole("button", { name: t("desks.strategies.go") }));
    expect(await screen.findByText(t("desks.strategies.symbolInvalid"))).toBeInTheDocument();
    expect(asked(stub)).toHaveLength(0);
  });

  it("a chain that cannot answer says No data from the source and shows no cards", async () => {
    const stub: Stub = { calls: [], ask: () => json({ ok: false, symbol: "AAPL", source: "Schwab", message: "Sin dato de Schwab: la cadena no tiene filas.", cards: [] }) };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    await screen.findByLabelText(t("desks.strategies.asset"));
    await userEvent.click(screen.getByRole("button", { name: t("desks.strategies.go") }));
    expect(await screen.findByText(/Sin dato de Schwab/)).toBeInTheDocument();
    expect(screen.queryByRole("article")).toBeNull();
  });

  it("opens with what the chief asked by chat, and asks the server only when the button is pressed", async () => {
    const stub: Stub = { calls: [], last: { form: { symbol: "NVDA", view: "bullish", target: null, budget: 2000, expiration: null, priority: "balanced" }, result: { ...RESULT, symbol: "NVDA" } } };
    stubApi(stub);
    renderWithApp(<StrategiesTab desk={DESK} />);
    await screen.findByRole("article", { name: t("desks.strategies.s.debit_call_spread") });
    expect(screen.getByLabelText(t("desks.strategies.asset"))).toHaveValue("NVDA");
    expect(screen.getByLabelText(t("desks.strategies.budget"))).toHaveValue("2000");
    expect(screen.getByLabelText(t("desks.strategies.view"))).toHaveValue("bullish");
    expect(asked(stub)).toHaveLength(0);                                                          // it showed the saved cards, it did not ask again
  });
});
