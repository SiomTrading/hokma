import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { renderWithApp, TEST_TOKEN } from "@/test/utils";
import { movedEnough } from "./optionDetailApi";
import { OptionDetailTab } from "./tabs/OptionDetailTab";
import { StrategiesTab } from "./tabs/StrategiesTab";
import type { DeskSummary } from "./types";

const DESK = {
  id: 4, project_id: 10, name: "Mesa A", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok", accounts: [],
} as unknown as DeskSummary;

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const SELECTION = { symbol: "AAPL", expiration: "2026-12-18", structure: "debit_call_spread", strikes: [200, 210], legs: null, quantity: 1, range_pct: null, iv_mult: null, strategy_key: null, name: null };

function detail(over: Record<string, unknown> = {}) {
  const prices = [92, 94, 96, 98, 100, 102, 104, 106, 108];
  return {
    ok: true, symbol: "AAPL", expiration: "2026-12-18", structure: "debit_call_spread", days: 28, spot: 100, source: "Schwab", as_of: "2026-11-20T15:00:00Z", delayed: false,
    legs: [{ instrument: "option", position: "long", kind: "call", strike: 100, premium: 3.5, quantity: 1, iv_used: 0.3 }, { instrument: "option", position: "short", kind: "call", strike: 110, premium: 0.2, quantity: 1, iv_used: 0.3 }],
    metrics: { net_premium_usd: 330, premium_type: "debit", max_profit_usd: 670, unbounded_profit: false, max_loss_usd: 330, unbounded_loss: false, breakevens: [103.3], p_profit: 0.41, expected_value_usd: 12.5 },
    table: {
      rows: prices.map((p, i) => ({ pct: p - 100, price: p, current: i === 4 })), range_pct: 8, spot_row: 4,
      columns: [{ days: 0, date: "2026-11-20", expiration: false }, { days: 14, date: "2026-12-04", expiration: false }, { days: 28, date: "2026-12-18", expiration: true }],
      pnl_usd: prices.map((p, i) => [(p - 103) * 20, (p - 103) * 30, i < 4 ? -330 : (p - 103.3) * 100 > 670 ? 670 : (p - 103.3) * 100]),
    },
    curves: { spots: [92, 100, 108], expiration_pnl_usd: [-330, -330, 470], mid_days: 14, mid_pnl_usd: [-300, -100, 500], mid_breakevens: [101.5], mid_breakevens_approximate: true, mid_breakevens_in_range_only: true },
    breakevens: [103.3], model: { iv_mult: 1, iv_mean: 0.3 },
    events: {
      expiration: { date: "2026-12-18", days: 28 },
      earnings: { kind: "earnings", ok: true, source: "Calendar", date: "2026-12-10", days_from_today: 20, before_expiration: true, relation: "before_expiration", message: "The next report is 2026-12-10, before expiration: the price may jump." },
      ex_dividend: { kind: "ex_dividend", ok: false, source: "la fecha ex-dividendo", date: null, relation: "no_data", message: "Sin dato de la fecha ex-dividendo: la fuente no respondio" },
    },
    recalc: { price_move_pct: 0.1, debounce_ms: 150, by_event_only: true, anchor_spot: 100 },
    limits: { range_pct: { min: 3, max: 20, default: 10 }, iv_mult: { min: 0.5, max: 2, default: 1 } }, warnings: [], ...over,
  };
}

const CARD = {
  id: "debit_call_spread", structure: "debit_call_spread", family: "bullish", net_premium_usd: 330, cost_usd: 330, credit_usd: 0, capital_required_usd: 330, max_profit_usd: 670, max_loss_usd: 330,
  unbounded_profit: false, unbounded_loss: false, breakevens: [103.3], p_profit: 0.41, expected_value_usd: 12.5, payoff: { spots: [80, 100, 120], pnl_usd: [-330, -330, 670] },
  status: "ok", status_message: null, locked: null, can_open_detail: true, verdict: { verdict: "recommended", label: "Recomendada", reason: "Probabilidad 41%.", backed_by: { name: "Caleb" } },
  legs: [{ position: "long", kind: "call", strike: 200, premium: 5.15 }, { position: "short", kind: "call", strike: 210, premium: 1.85 }],
};
const LOCKED = { ...CARD, id: "iron_condor", structure: "iron_condor", status: "locked", locked: "premium_selling", can_open_detail: false, status_message: "La venta de prima esta cerrada en esta mesa para AAPL.",
  verdict: { verdict: "closed", label: "Cerrada", reason: "La venta de prima esta cerrada en esta mesa para AAPL.", backed_by: { source: "premium_switch" } },
  legs: [{ position: "long", kind: "put", strike: 170 }, { position: "short", kind: "put", strike: 180 }, { position: "short", kind: "call", strike: 210 }, { position: "long", kind: "call", strike: 220 }] };
const STRATEGIES = { form: { symbol: "AAPL", view: "bullish", target: null, budget: 3000, expiration: "2026-12-18", priority: "balanced" },
  result: { ok: true, symbol: "AAPL", view: "bullish", expiration: "2026-12-18", days: 28, spot: 200, source: "Schwab", as_of: "2026-11-20T15:00:00Z", notes: [], vol_view_source: "vol_analysis", cards: [CARD, LOCKED] } };

interface Stub {
  selection?: unknown;
  saved?: unknown[];
  last?: unknown;
  ask?: (body: Record<string, unknown>) => Response;
  propose?: (body: Record<string, unknown>) => Response;
  calls: Array<{ url: string; method: string; body: Record<string, unknown> | null }>;
}

function stubApi(stub: Stub) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
    stub.calls.push({ url: String(url), method, body });
    const u = String(url);
    if (u.endsWith("/option-detail/selection")) return method === "PUT" ? json({ selection: body }) : json({ selection: stub.selection ?? null });
    if (u.endsWith("/option-detail/saved")) return method === "POST" ? json({ key: "mi-spread", name: body?.name, ...body }, 201) : json({ strategies: stub.saved ?? [] });
    if (u.endsWith("/option-detail/propose")) return stub.propose ? stub.propose(body ?? {}) : json({ status: "awaiting_owner", code: "awaiting_owner", why: "Esperando tu aprobacion." });
    if (u.endsWith("/option-detail")) return stub.ask ? stub.ask(body ?? {}) : json(detail());
    if (u.endsWith("/strategies/last")) return json(stub.last ?? { form: null, result: null });
    if (u.includes("/strategies/") && u.endsWith("/account")) return json({ ok: true });
    if (u.endsWith("/api/brokers")) return json({ brokers: [{ id: 1, name: "Simulado", kind: "sim", commission: { type: "bps", value: 0, min: 0 }, slippage_bps: 0 }] });
    if (u.includes("/api/brokers/1/accounts")) return json({ accounts: [] });
    if (u.endsWith("/api/desks")) return json({ desks: [DESK] });
    if (u.includes("/options/expirations")) return json({ expirations: [] });
    return json({ id: 4, name: "Mesa A", assets: [{ symbol: "AAPL", note: null }], connections: [] });
  }));
}

const asked = (stub: Stub) => stub.calls.filter((c) => c.method === "POST" && c.url.endsWith("/option-detail"));
const proposed = (stub: Stub) => stub.calls.filter((c) => c.url.endsWith("/option-detail/propose"));

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
  vi.useRealTimers();
});

describe("Option detail panel", () => {
  it("opens with the strategy of the desk and shows the figures, the table by date and price, the chart and the events", async () => {
    const stub: Stub = { calls: [], selection: SELECTION };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    expect(asked(stub)).toHaveLength(1);
    expect(asked(stub)[0]!.body).toMatchObject({ symbol: "AAPL", expiration: "2026-12-18", structure: "debit_call_spread", strikes: [200, 210], quantity: 1, range_pct: 10, iv_mult: 1 });
    const grid = screen.getByRole("table", { name: t("desks.detail.grid") });
    expect(within(grid).getAllByRole("row")).toHaveLength(1 + 9);                                          // the header and the nine price rows
    const current = grid.querySelector("tr.opd-current")!;
    expect(current).toHaveAttribute("aria-current", "true");
    expect(within(current as HTMLElement).getByText("100.00")).toBeInTheDocument();                       // the row of the current price is the highlighted one
    expect(screen.getByText(t("desks.detail.debit"))).toBeInTheDocument();
    expect(screen.getByText("103.30")).toBeInTheDocument();                                                // the breakeven at expiration
    expect(screen.getByText("41.0%")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: t("desks.detail.chart") })).toBeInTheDocument();
    // the cells carry the sign as a class
    expect(grid.querySelectorAll("td.up").length).toBeGreaterThan(0);
    expect(grid.querySelectorAll("td.down").length).toBeGreaterThan(0);
  });

  it("D7: the next report before expiration is marked and a source without data says so, never a blank", async () => {
    stubApi({ calls: [], selection: SELECTION });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    const events = await screen.findByRole("group", { name: t("desks.detail.events") });
    expect(within(events).getByText(t("desks.detail.beforeExpiration"))).toBeInTheDocument();
    expect(within(events).getByText(/The next report is 2026-12-10/)).toBeInTheDocument();
    expect(within(events).getByText(/Sin dato de la fecha ex-dividendo/)).toBeInTheDocument();
    expect(events.querySelectorAll(".opd-event.warn")).toHaveLength(1);
  });

  it("D5: two breakevens at expiration are two values; the one at half of the term says it is approximate", async () => {
    const condor = detail({ structure: "iron_condor", metrics: { ...detail().metrics, breakevens: [92.5, 107.5] } });
    stubApi({ calls: [], selection: { ...SELECTION, structure: "iron_condor", strikes: [85, 95, 105, 115] }, ask: () => json(condor) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    expect(screen.getByText(t("desks.detail.breakevens"))).toBeInTheDocument();
    expect(screen.getByText("92.50 / 107.50")).toBeInTheDocument();
    expect(screen.getByText(/Equilibrio a mitad de plazo: 101.50/)).toBeInTheDocument();
    expect(screen.getByText(t("desks.detail.approximate"), { exact: false })).toBeInTheDocument();
  });

  it("D2 and D3: the sliders ask only when they are released, with the value the owner left", async () => {
    const stub: Stub = { calls: [], selection: SELECTION };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    const range = screen.getByLabelText(t("desks.detail.rangeLabel"));
    const iv = screen.getByLabelText(t("desks.detail.ivLabel"));
    expect(range).toHaveAttribute("min", "3");
    expect(range).toHaveAttribute("max", "20");
    expect(iv).toHaveAttribute("min", "0.5");
    expect(iv).toHaveAttribute("max", "2");
    fireEvent.change(range, { target: { value: "3" } });
    fireEvent.change(iv, { target: { value: "1.5" } });
    await new Promise((r) => setTimeout(r, 400));
    expect(asked(stub)).toHaveLength(1);                                                                   // dragging asks nothing
    expect(screen.getByText(t("desks.detail.range", { n: 3 }))).toBeInTheDocument();
    fireEvent.pointerUp(iv);
    await waitFor(() => expect(asked(stub)).toHaveLength(2));
    expect(asked(stub)[1]!.body).toMatchObject({ range_pct: 3, iv_mult: 1.5 });
    fireEvent.keyUp(range);                                                                                // the same values again: no new request
    await new Promise((r) => setTimeout(r, 400));
    expect(asked(stub)).toHaveLength(2);
  });

  it("D4: the price of the stream asks again only when it moves more than the percent of the server, and the highlighted row follows", async () => {
    const moved = detail({ spot: 101, recalc: { price_move_pct: 0.1, debounce_ms: 150, by_event_only: true, anchor_spot: 101 } });
    const stub: Stub = { calls: [], selection: SELECTION };
    let n = 0;
    stub.ask = () => json(++n === 1 ? detail() : moved);
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(0));
    const socket = FakeSocket.of("/ws/market")[0]!;
    act(() => socket.serverOpen());
    const sub = String(socket.ops("subscribe")[0]!.sub);
    act(() => socket.serverSend({ type: "snapshot", sub, seq: 1, epoch: "e1", last: 100.05, previous_close: 99, change_pct: 1, source: "Schwab", data_state: "live" }));
    await new Promise((r) => setTimeout(r, 300));
    expect(asked(stub)).toHaveLength(1);                                                                   // 0.05 percent: nothing
    expect(movedEnough(100, 100.05, 0.1)).toBe(false);
    expect(movedEnough(100, 100.2, 0.1)).toBe(true);
    act(() => socket.serverSend({ type: "last", sub, seq: 2, epoch: "e1", price: 101, change_pct: 2, source: "Schwab", data_state: "live" }));
    await waitFor(() => expect(asked(stub)).toHaveLength(2));
    expect(await screen.findByLabelText(t("desks.strategies.price"))).toHaveTextContent("101");
    await new Promise((r) => setTimeout(r, 300));
    expect(asked(stub)).toHaveLength(2);                                                                   // the new answer is the new anchor: no loop
  });

  it("without data the panel says Sin dato de la fuente and draws no table", async () => {
    stubApi({ calls: [], selection: SELECTION, ask: () => json({ ok: false, symbol: "AAPL", expiration: "2026-12-18", source: "Schwab", message: "Sin dato de Schwab: la cadena no tiene filas." }) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    expect(await screen.findByText(/Sin dato de Schwab/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("an invalid request shows the message of the server and keeps the panel", async () => {
    stubApi({ calls: [], selection: SELECTION, ask: () => json({ detail: { code: "bad_option_detail_args", message: "La consulta del detalle de opcion no es valida." } }, 422) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/no es valida/);
  });

  it("with no strategy open it says how to open one and asks nothing", async () => {
    const stub: Stub = { calls: [] };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    expect(await screen.findByText(t("desks.detail.empty"))).toBeInTheDocument();
    expect(asked(stub)).toHaveLength(0);
    expect(screen.queryByRole("button", { name: t("desks.detail.propose") })).toBeNull();
  });

  it("the selector lists the saved strategies and the cards of Strategies, and choosing one opens it", async () => {
    const stub: Stub = { calls: [], selection: SELECTION, last: STRATEGIES,
      saved: [{ key: "mi-put", name: "Mi put", symbol: "AAPL", expiration: "2026-12-18", structure: "long_put", strikes: [190], legs: null, quantity: 2 }] };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    const select = screen.getByLabelText(t("desks.detail.selector"));
    expect(within(select).getByRole("option", { name: "Mi put" })).toBeInTheDocument();
    expect(within(select).getAllByRole("option").some((o) => /Compra de spread call|Debit call spread|spread/i.test(o.textContent ?? ""))).toBe(true);
    expect(within(select).queryByText(/iron condor/i)).toBeNull();                                           // a closed card is not offered
    await userEvent.selectOptions(select, "saved:mi-put");
    await waitFor(() => expect(asked(stub)).toHaveLength(2));
    expect(asked(stub)[1]!.body).toMatchObject({ structure: "long_put", strikes: [190], quantity: 2 });
    expect(stub.calls.some((c) => c.method === "PUT" && c.url.endsWith("/option-detail/selection") && c.body?.strategy_key === "mi-put")).toBe(true);
  });

  it("Save keeps the strategy under its name and says so", async () => {
    const stub: Stub = { calls: [], selection: SELECTION };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    await userEvent.click(screen.getByRole("button", { name: t("desks.detail.save") }));
    await userEvent.type(screen.getByLabelText(t("desks.detail.saveName")), "Mi spread");
    await userEvent.click(screen.getByRole("button", { name: t("desks.detail.saveConfirm") }));
    expect(await screen.findByText(t("desks.detail.saved", { name: "Mi spread" }))).toBeInTheDocument();
    const post = stub.calls.find((c) => c.method === "POST" && c.url.endsWith("/option-detail/saved"))!;
    expect(post.body).toMatchObject({ name: "Mi spread", symbol: "AAPL", structure: "debit_call_spread", strikes: [200, 210] });
  });
});

describe("Propose from the Option detail panel", () => {
  const press = async () => userEvent.click(await screen.findByRole("button", { name: t("desks.detail.propose") }));

  it("sends the strategy with its strategy_key and says it waits for the owner; it never says sent", async () => {
    const stub: Stub = { calls: [], selection: SELECTION };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    expect(await screen.findByText(t("desks.detail.status.awaiting_owner"))).toBeInTheDocument();
    expect(proposed(stub)[0]!.body).toMatchObject({ symbol: "AAPL", structure: "debit_call_spread", strikes: [200, 210], quantity: 1, strategy_key: "principal" });
    expect(screen.queryByText(t("desks.detail.status.sent"))).toBeNull();
  });

  it("a second press on a proposal that is already waiting does not create a duplicate", async () => {
    const stub: Stub = { calls: [], selection: SELECTION };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    expect(await screen.findByText(t("desks.detail.status.awaiting_owner"))).toBeInTheDocument();
    const button = screen.getByRole("button", { name: t("desks.detail.propose") });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(proposed(stub)).toHaveLength(1);
  });

  it("a rule of the owner that blocks it shows its why", async () => {
    stubApi({ calls: [], selection: SELECTION, propose: () => json({ status: "refused", code: "size_premium", why: "Tu regla limita la prima a 50 USD por orden." }) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    expect(await screen.findByText(/Tu regla limita la prima a 50 USD/)).toBeInTheDocument();
    expect(screen.getByText(t("desks.detail.status.refused"))).toBeInTheDocument();
  });

  it("premium selling shows its lock and the reason, and the panel keeps the numbers", async () => {
    stubApi({ calls: [], selection: { ...SELECTION, structure: "iron_condor", strikes: [85, 95, 105, 115] },
      propose: () => json({ status: "refused", code: "premium_selling_closed", locked: "premium_selling", why: "La venta de prima sobre AAPL esta cerrada para esta mesa." }) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await screen.findByRole("table", { name: t("desks.detail.grid") });
    await press();
    expect(await screen.findByText(/La venta de prima sobre AAPL esta cerrada/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: t("desks.detail.grid") })).toBeInTheDocument();
  });

  it("D6: a desk without account opens Add account in the panel, and the proposal is made again when the account is added", async () => {
    const stub: Stub = { calls: [], selection: SELECTION,
      propose: () => json({ status: "needs_account", code: "no_account", why: "Esta mesa no tiene cuenta. Agrega una para poder proponer.", strategy_key: "principal", accounts: [] }) };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    expect(await screen.findByText(/Esta mesa no tiene cuenta/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("desks.detail.addAccount") }));
    expect(await screen.findByRole("dialog", { name: t("desks.accounts.add.title", { name: "Mesa A" }) })).toBeInTheDocument();
    expect(proposed(stub)).toHaveLength(1);
  });

  it("D6: a strategy without account asks which one of the desk, binds it and proposes again", async () => {
    let n = 0;
    const stub: Stub = { calls: [], selection: { ...SELECTION, strategy_key: "mi-spread", name: "Mi spread" },
      propose: () => json(++n === 1
        ? { status: "needs_account", code: "strategy_without_account", why: "Esta estrategia no tiene una cuenta atada. Elige por cual cuenta de la mesa sale.", strategy_key: "mi-spread",
            accounts: [{ id: 7, name: "Cuenta A", broker: "Simulado", mask: null }, { id: 8, name: "Cuenta B", broker: "Simulado", mask: null }] }
        : { status: "awaiting_owner", code: "awaiting_owner", why: "Esperando tu aprobacion." }) };
    stubApi(stub);
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    const pick = await screen.findByRole("combobox", { name: t("desks.detail.pickAccount") });
    await userEvent.selectOptions(pick, "8");
    await userEvent.click(screen.getByRole("button", { name: t("desks.detail.useAccount") }));
    expect(await screen.findByText(t("desks.detail.status.awaiting_owner"))).toBeInTheDocument();
    const bind = stub.calls.find((c) => c.method === "PUT" && c.url.endsWith("/strategies/mi-spread/account"))!;
    expect(bind.body).toEqual({ account_id: 8 });
    expect(proposed(stub)).toHaveLength(2);
    expect(proposed(stub)[1]!.body).toMatchObject({ strategy_key: "mi-spread" });
  });

  it("a failed request shows its message and leaves the button usable", async () => {
    stubApi({ calls: [], selection: SELECTION, propose: () => json({ detail: { code: "bad_option_detail_args", message: "La consulta del detalle de opcion no es valida." } }, 422) });
    renderWithApp(<OptionDetailTab desk={DESK} />);
    await press();
    expect(await screen.findByText(/no es valida/, { selector: "p.strat-error" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.detail.propose") })).toBeEnabled();
  });
});

describe("Strategies panel: Open detail and Propose", () => {
  const stubWith = (stub: Stub) => stubApi({ ...stub, last: STRATEGIES });

  it("Open detail keeps the strategy of the card as the selection and opens the panel", async () => {
    const stub: Stub = { calls: [] };
    stubWith(stub);
    const openPanel = vi.fn();
    renderWithApp(<StrategiesTab desk={DESK} openPanel={openPanel} />);
    const card = await screen.findByRole("article", { name: t("desks.strategies.s.debit_call_spread") });
    await userEvent.click(within(card).getByRole("button", { name: t("desks.strategies.openDetail") }));
    await waitFor(() => expect(openPanel).toHaveBeenCalledWith("option_detail"));
    const put = stub.calls.find((c) => c.method === "PUT" && c.url.endsWith("/option-detail/selection"))!;
    expect(put.body).toMatchObject({ symbol: "AAPL", expiration: "2026-12-18", structure: "debit_call_spread", strikes: [200, 210], quantity: 1 });
  });

  it("a closed card has no Open detail and its Propose is off with the reason; no request is made", async () => {
    const stub: Stub = { calls: [] };
    stubWith(stub);
    renderWithApp(<StrategiesTab desk={DESK} openPanel={vi.fn()} />);
    const locked = await screen.findByRole("article", { name: t("desks.strategies.s.iron_condor") });
    expect(within(locked).queryByRole("button", { name: t("desks.strategies.openDetail") })).toBeNull();
    const button = within(locked).getByRole("button", { name: t("desks.detail.propose") });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "La venta de prima esta cerrada en esta mesa para AAPL.");
    expect(proposed(stub)).toHaveLength(0);
  });

  it("Propose of a card goes with its structure and strikes, and shows what the door answered", async () => {
    const stub: Stub = { calls: [], propose: () => json({ status: "analysis_only", code: "level_1", why: "La mesa esta en nivel 1: solo analiza." }) };
    stubWith(stub);
    renderWithApp(<StrategiesTab desk={DESK} openPanel={vi.fn()} />);
    const card = await screen.findByRole("article", { name: t("desks.strategies.s.debit_call_spread") });
    await userEvent.click(within(card).getByRole("button", { name: t("desks.detail.propose") }));
    expect(await within(card).findByText(/nivel 1: solo analiza/)).toBeInTheDocument();
    expect(proposed(stub)[0]!.body).toEqual({ symbol: "AAPL", expiration: "2026-12-18", structure: "debit_call_spread", strikes: [200, 210], legs: null, quantity: 1, strategy_key: "principal" });
  });
});
