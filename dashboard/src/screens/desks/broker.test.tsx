import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderShell } from "@/test/utils";
import type { Account } from "./accountsApi";
import { resetDesksStore } from "./store";
import { TODAY_EMPTY } from "./todayFixture";
import type { DeskSummary } from "./types";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });

function desk(patch: Partial<DeskSummary>): DeskSummary {
  return {
    id: 1, project_id: 10, name: "Swing", focus: null, capital: 10000, currency: "USD", status: "active", template: "standard",
    today: 0, drawdown: null, agents_total: 7, agents_working: 0, tokens_week: 0, token_budget: null, budget_state: "ok",
    ...patch,
  };
}

function account(patch: Partial<Account>): Account {
  return {
    id: 1, broker_id: 1, broker: "Hokma Simulated", name: "Cuenta A", initial_capital: 25000, currency: "USD", status: "active",
    desk_id: null, cash: 25000, equity: 25000, as_of: "2026-11-18T00:00:00Z", ...patch,
  };
}

interface Call { url: string; method: string; body: unknown }

function stubApi(state: { desks: DeskSummary[]; accounts: Account[]; brokers?: unknown[] }, failReset = false) {
  const calls: Call[] = [];
  const brokers = state.brokers ?? [{ id: 1, kind: "sim", name: "Hokma Simulated", commission: { type: "bps", value: 1, min: 0 }, slippage_bps: 2 }];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ url: String(url), method, body });
      if (url === "/api/desks") return json({ desks: state.desks });
      if (url === "/api/today") return json(TODAY_EMPTY);
      if (url === "/api/brokers") return json({ brokers });
      if (/^\/api\/brokers\/1\/accounts$/.test(url) && method === "GET") return json({ accounts: state.accounts });
      if (/^\/api\/brokers\/1\/accounts$/.test(url) && method === "POST") {
        const made = account({ id: 50 + state.accounts.length, name: (body as { name: string }).name, initial_capital: (body as { capital: number }).capital });
        state.accounts.push(made);
        return json(made, 201);
      }
      if (url === "/api/brokers/1/costs" && method === "PUT") return json({ id: 1 });
      if (/^\/api\/desks\/\d+\/account$/.test(url) && method === "DELETE") {
        const id = Number(url.split("/")[3]);
        state.accounts.forEach((x) => { if (x.desk_id === id) x.desk_id = null; });
        return json({ desk_id: id, account: null });
      }
      if (/\/reset$/.test(url)) return failReset ? json({ detail: { code: "x", message: "No se pudo reiniciar." } }, 500) : json(state.accounts[0]);
      if (/^\/api\/desks\/\d+\/account$/.test(url) && method === "PUT") {
        const id = Number(url.split("/")[3]);
        const a = state.accounts.find((x) => x.id === (body as { account_id: number }).account_id)!;
        a.desk_id = id;
        state.desks.find((d) => d.id === id)!.account = { id: a.id, name: a.name, broker: a.broker, initial_capital: a.initial_capital, balance: { cash: a.cash, equity: a.equity, currency: "USD" }, as_of: a.as_of };
        return json({ id: a.id });
      }
      return json({});
    }),
  );
  return calls;
}

const openBroker = async () => {
  await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
  await userEvent.click(await screen.findByRole("tab", { name: t("desks.fund.broker") }));
};

beforeEach(() => {
  setLocale("es");
  resetDesksStore();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Broker tab", () => {
  it("shows a text and a button, never an empty screen, when there is no account", async () => {
    stubApi({ desks: [desk({})], accounts: [] });
    renderShell();
    await openBroker();
    expect(await screen.findByText(t("desks.accounts.empty.title"))).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: t("desks.accounts.empty.button") })).toHaveLength(1);
    expect(screen.queryByText(/paso \d/i)).not.toBeInTheDocument();
  });

  it("says what the Hokma broker is, lets the costs be edited and says external brokers arrive with the orders, with no fake button", async () => {
    const calls = stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    expect(await screen.findByText(t("desks.broker.connected"))).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Hokma Simulated/ })).toBeInTheDocument();
    const connect = screen.getByRole("region", { name: t("desks.broker.external") });
    expect(connect).toHaveTextContent(t("desks.broker.externalText"));
    expect(within(connect).queryByRole("button")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("desks.broker.editCosts") }));
    const fee = screen.getByLabelText(t("desks.accounts.commission"));
    await userEvent.clear(fee);
    await userEvent.type(fee, "3");
    await userEvent.click(screen.getByRole("button", { name: t("desks.broker.saveCosts") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT" && c.url === "/api/brokers/1/costs")?.body).toEqual({ commission: { type: "bps", value: 3, min: 0 }, slippage_bps: 2 }));
  });

  it("refuses negative costs without calling the server", async () => {
    const calls = stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.broker.editCosts") }));
    await userEvent.type(screen.getByLabelText(t("desks.accounts.slippage")), "0");
    await userEvent.clear(screen.getByLabelText(t("desks.accounts.slippage")));
    await userEvent.type(screen.getByLabelText(t("desks.accounts.slippage")), "-1");
    expect(screen.getByRole("button", { name: t("desks.broker.saveCosts") })).toBeDisabled();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("without any broker it says so and cannot create an account", async () => {
    stubApi({ desks: [], accounts: [], brokers: [] });
    renderShell();
    await openBroker();
    expect(await screen.findByText(t("desks.accounts.noBroker.text"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("desks.accounts.newAccount") })).toBeDisabled();
  });

  it("lists accounts with capital, balance and desk, and says 'No disponible' for a null balance", async () => {
    stubApi({ desks: [desk({ id: 1 })], accounts: [account({ id: 1, desk_id: 1 }), account({ id: 2, name: "Cuenta B", equity: null, cash: null })] });
    renderShell();
    await openBroker();
    const row = (await screen.findByRole("row", { name: /Cuenta A/ }));
    expect(row).toHaveTextContent("25.000");
    expect(row).toHaveTextContent("Swing");
    expect(screen.getByRole("row", { name: /Cuenta B/ })).toHaveTextContent(t("desks.accounts.unavailable"));
    expect(screen.getByRole("row", { name: /Cuenta B/ })).toHaveTextContent(t("desks.accounts.free"));
  });

  it("creates an account with the typed name and capital", async () => {
    const calls = stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.newAccount") }));
    const dialog = await screen.findByRole("dialog", { name: t("desks.accounts.newAccount.title") });
    const save = within(dialog).getByRole("button", { name: t("desks.accounts.newAccount.save") });
    expect(save).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.name")), "Cuenta C");
    await userEvent.clear(within(dialog).getByLabelText(t("desks.accounts.capital")));
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.capital")), "5000");
    await userEvent.click(save);
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/brokers/1/accounts")?.body).toEqual({ name: "Cuenta C", capital: 5000 }));
  });

  it("refuses a capital outside the limits", async () => {
    stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.newAccount") }));
    const dialog = await screen.findByRole("dialog", { name: t("desks.accounts.newAccount.title") });
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.name")), "X");
    await userEvent.clear(within(dialog).getByLabelText(t("desks.accounts.capital")));
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.capital")), "50");
    expect(within(dialog).getByRole("button", { name: t("desks.accounts.newAccount.save") })).toBeDisabled();
  });

  it("asks before a reset, says the capital it returns to, and sends confirmed", async () => {
    const calls = stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.reset") }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("25.000 USD");
    expect(calls.some((c) => c.url.endsWith("/reset"))).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.reset.confirm") }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/accounts/1/reset")?.body).toEqual({ confirmed: true }));
  });

  it("takes an assigned account off its desk after a confirmation, and an unassigned one has no such button", async () => {
    const calls = stubApi({ desks: [desk({})], accounts: [account({ desk_id: 1 }), account({ id: 2, name: "Cuenta B" })] });
    renderShell();
    await openBroker();
    const buttons = await screen.findAllByRole("button", { name: t("desks.accounts.unassign") });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]!);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Swing");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.unassign.confirm") }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/desks/1/account")).toBe(true));
    await waitFor(() => expect(screen.queryByRole("button", { name: t("desks.accounts.unassign") })).toBeNull());
  });

  it("shows the currency next to the balance", async () => {
    stubApi({ desks: [], accounts: [account({ equity: 25500 })] });
    renderShell();
    await openBroker();
    expect(await screen.findByText("25.500,00 USD")).toBeInTheDocument();
  });

  it("shows the server's message when a reset fails and keeps the dialog", async () => {
    stubApi({ desks: [], accounts: [account({})] }, true);
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.reset") }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.reset.confirm") }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No se pudo reiniciar.");
  });

  it("assigns an account to a desk from the accounts table", async () => {
    const calls = stubApi({ desks: [desk({ id: 7 })], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.assign") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.assign.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")).toMatchObject({ url: "/api/desks/7/account", body: { account_id: 1 } }));
  });
});

describe("account in the desk card", () => {
  it("shows 'Sin cuenta' with a button that assigns a free account, then the balance", async () => {
    const calls = stubApi({ desks: [desk({ id: 3 })], accounts: [account({ id: 4 })] });
    renderShell();
    await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
    const card = await screen.findByRole("button", { name: "Abrir la mesa Swing" });
    expect(card).toHaveTextContent("Sin cuenta");
    await userEvent.click(screen.getByRole("button", { name: t("desks.card.assign") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("button", { name: t("desks.accounts.assign.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ account_id: 4 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Abrir la mesa Swing" })).toHaveTextContent(/Cuenta A\s*Hokma Simulated/));
  });

  it("says there are no free accounts instead of an empty selector", async () => {
    stubApi({ desks: [desk({ id: 3 })], accounts: [] });
    renderShell();
    await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
    await userEvent.click(await screen.findByRole("button", { name: t("desks.card.assign") }));
    expect(await screen.findByText(t("desks.accounts.assign.noAccounts"))).toBeInTheDocument();
  });
});
