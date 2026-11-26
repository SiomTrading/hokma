import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderShell } from "@/test/utils";
import { resetDesksStore } from "./store";
import { TODAY_EMPTY } from "./todayFixture";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

const DESK = {
  id: 1, project_id: 10, name: "Swing", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
  today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
};
const BROKERS = [
  { id: 1, kind: "sim", name: "Hokma", commission: { type: "bps", value: 1, min: 0 }, slippage_bps: 2 },
  { id: 2, kind: "schwab", name: "Schwab", commission: { type: "bps", value: 0, min: 0 }, slippage_bps: 0, orders_enabled: true },
];
const SCHWAB_ACCOUNT = { id: 7, broker_id: 2, broker: "Schwab", name: "Schwab ••••1234", initial_capital: 5000, currency: "USD", status: "active", desk_ids: [1], cash: 100, equity: 5000, as_of: "2026-11-18T00:00:00Z", can_reset: false, mask: "••••1234" };

const future = (min: number) => new Date(Date.now() + min * 60000).toISOString().replace("T", " ").slice(0, 19);
const pending = (id: number, patch: Record<string, unknown> = {}) => ({
  decision_id: id, action: "place", agent: "trader", reason: "Breakout above resistance", why: "Order above the size limit", replaces_order_id: null,
  created_at: "2026-11-21 10:00:00", expires_at: future(25),
  order: { orderType: "LIMIT", price: 210.5, orderStrategyType: "SINGLE", orderLegCollection: [{ instruction: "BUY", quantity: 10, instrument: { symbol: "AAPL" } }] },
  ...patch,
});

function stub(state: { pending: unknown[]; approve?: () => Response }) {
  const calls: Array<{ url: string; method: string }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url: String(url), method });
    if (url === "/api/desks") return json({ desks: [DESK] });
    if (url === "/api/today") return json(TODAY_EMPTY);
    if (url === "/api/brokers") return json({ brokers: BROKERS });
    if (url === "/api/brokers/1/accounts") return json({ accounts: [] });
    if (url === "/api/brokers/2/accounts") return json({ accounts: [SCHWAB_ACCOUNT] });
    if (url === "/api/desks/1/orders/pending" && method === "GET") return json({ pending: state.pending });
    if (/\/orders\/pending\/\d+\/approve$/.test(url) && method === "POST") { const out = state.approve!(); state.pending = []; return out; }
    if (/\/orders\/pending\/\d+\/reject$/.test(url) && method === "POST") { state.pending = []; return json({ status: "rejected", decision_id: 1 }); }
    return json({});
  }));
  return calls;
}

const openBroker = async () => {
  await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
  await userEvent.click(await screen.findByRole("tab", { name: t("desks.fund.broker") }));
};

beforeEach(() => { setLocale("es"); resetDesksStore(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Pending orders in the Broker tab (SPEC-015 18.3)", () => {
  it("lists the order with desk, summary, reasons and time left", async () => {
    stub({ pending: [pending(5)] });
    renderShell();
    await openBroker();
    const section = await screen.findByRole("region", { name: t("desks.pending.title") });
    expect(section).toHaveTextContent("Swing ••••1234");
    expect(section).toHaveTextContent("BUY 10 AAPL LIMIT 210.5");
    expect(section).toHaveTextContent("Breakout above resistance");
    expect(section).toHaveTextContent("Order above the size limit");
    expect(section).toHaveTextContent(/(Vence en|Expires in) 2[5-6] min/);
    expect(within(section).getByRole("button", { name: new RegExp(t("desks.pending.approve")) })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: new RegExp(t("desks.pending.reject")) })).toBeInTheDocument();
    expect(section.textContent).not.toMatch(/simulad/i);
  });

  it("is not shown when nothing waits", async () => {
    stub({ pending: [] });
    renderShell();
    await openBroker();
    await screen.findByText(t("desks.broker.connected"));
    expect(screen.queryByText(t("desks.pending.title"))).not.toBeInTheDocument();
  });

  it("approves, says it was sent and reloads the list", async () => {
    const calls = stub({ pending: [pending(5)], approve: () => json({ status: "sent", order_id: "123", decision_id: 5 }) });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(t("desks.pending.approve")) }));
    expect(await screen.findByRole("status")).toHaveTextContent(t("desks.pending.sent"));
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/desks/1/orders/pending/5/approve")).toBe(true);
    await waitFor(() => expect(screen.queryByRole("button", { name: new RegExp(t("desks.pending.approve")) })).not.toBeInTheDocument());
  });

  it("shows why it was not sent when the new check does not pass", async () => {
    stub({ pending: [pending(5)], approve: () => json({ status: "not_sent", why: "The price moved away from the limit.", decision_id: 5 }) });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(t("desks.pending.approve")) }));
    expect(await screen.findByRole("status")).toHaveTextContent(t("desks.pending.notSent", { why: "The price moved away from the limit." }));
  });

  it("says it expired on a 409 decision_expired, with the text of the server", async () => {
    stub({ pending: [pending(5)], approve: () => json({ detail: { code: "decision_expired", message: "The decision expired after 30 min." } }, 409) });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(t("desks.pending.approve")) }));
    const out = await screen.findByRole("status");
    expect(out).toHaveTextContent(t("desks.pending.expired", { message: "" }).trim());
    expect(out).toHaveTextContent("The decision expired after 30 min.");
  });

  it("rejects and says nothing was sent", async () => {
    const calls = stub({ pending: [pending(5)] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: new RegExp(t("desks.pending.reject")) }));
    expect(await screen.findByRole("status")).toHaveTextContent(t("desks.pending.rejected"));
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/desks/1/orders/pending/5/reject")).toBe(true);
  });

  it("has the same keys in es and en, with no dash and no 'simulated' wording", () => {
    const keys = (l: "es" | "en") => Object.keys(getCatalog(l)).filter((k) => k.startsWith("desks.pending.")).sort();
    expect(keys("es").length).toBeGreaterThan(10);
    expect(keys("en")).toEqual(keys("es"));
    for (const l of ["es", "en"] as const) {
      for (const k of keys(l)) {
        const text = getCatalog(l)[k];
        expect(text).not.toContain("\u2014");
        expect(text).not.toMatch(/simulad|simulat/i);
      }
    }
  });
});
