import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { ACCOUNT_PANEL } from "@/test/connectionsBackend";
import { renderShell, renderWithApp } from "@/test/utils";
import { SchwabAccount } from "@/screens/connections/SchwabAccount";
import type { Account } from "./accountsApi";
import { DeskBar } from "./DeskBar";
import { resetDesksStore } from "./store";
import { TODAY_EMPTY } from "./todayFixture";
import { DESK_TABS } from "./types";
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
    desk_ids: [], cash: 25000, equity: 25000, as_of: "2026-11-18T00:00:00Z", ...patch,
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
      const listed = /^\/api\/brokers\/(\d+)\/accounts$/.exec(url);
      if (listed && method === "GET") return json({ accounts: state.accounts.filter((a) => a.broker_id === Number(listed[1])) });
      const switched = /^\/api\/brokers\/(\d+)\/orders-enabled$/.exec(url);
      if (switched && method === "PUT") {
        const found = (brokers as Array<{ id: number; orders_enabled?: boolean }>).find((b) => b.id === Number(switched[1]))!;
        found.orders_enabled = (body as { enabled: boolean }).enabled;
        return json(found);
      }
      if (url === "/api/connections/schwab/account") {
        const schwab = (brokers as Array<{ kind: string; orders_enabled?: boolean }>).find((b) => b.kind === "schwab");
        return json({ ...ACCOUNT_PANEL, orders_enabled: schwab?.orders_enabled ?? false });
      }
      if (/^\/api\/brokers\/1\/accounts$/.test(url) && method === "POST") {
        const made = account({ id: 50 + state.accounts.length, name: (body as { name: string }).name, initial_capital: (body as { capital: number }).capital });
        state.accounts.push(made);
        return json(made, 201);
      }
      if (url === "/api/brokers/1/costs" && method === "PUT") return json({ id: 1 });
      const removed = /^\/api\/desks\/(\d+)\/accounts\/(\d+)$/.exec(url);
      if (removed && method === "DELETE") {
        const [id, accountId] = [Number(removed[1]), Number(removed[2])];
        state.accounts.forEach((x) => { if (x.id === accountId) x.desk_ids = x.desk_ids.filter((d) => d !== id); });
        return json({ desk_id: id, account_id: accountId, accounts: [] });
      }
      if (/\/reset$/.test(url)) return failReset ? json({ detail: { code: "x", message: "No se pudo reiniciar." } }, 500) : json(state.accounts[0]);
      const added = /^\/api\/desks\/(\d+)\/accounts$/.exec(url);
      if (added && method === "POST") {
        const id = Number(added[1]);
        const sent = body as { account_id?: number; demo?: { name: string; initial_capital: number } };
        const a = sent.demo ? account({ id: 60 + state.accounts.length, name: sent.demo.name, initial_capital: sent.demo.initial_capital }) : state.accounts.find((x) => x.id === sent.account_id)!;
        if (sent.demo) state.accounts.push(a);
        a.desk_ids = [...a.desk_ids, id];
        const mine = state.desks.find((d) => d.id === id)!;
        mine.accounts = [...(mine.accounts ?? []), { id: a.id, name: a.name, broker: a.broker, initial_capital: a.initial_capital, balance: { cash: a.cash, equity: a.equity, currency: "USD" }, as_of: a.as_of, shared_with: a.desk_ids.filter((d) => d !== id), strategies: [] }];
        return json({ id: a.id }, 201);
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
    expect(connect).toHaveTextContent(t("desks.broker.externalText2"));
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
    stubApi({ desks: [desk({ id: 1 })], accounts: [account({ id: 1, desk_ids: [1] }), account({ id: 2, name: "Cuenta B", equity: null, cash: null })] });
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

  it("starts with the capital empty and the save button off until it has a valid value", async () => {
    stubApi({ desks: [], accounts: [account({})] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.newAccount") }));
    const dialog = await screen.findByRole("dialog", { name: t("desks.accounts.newAccount.title") });
    expect(within(dialog).getByLabelText(t("desks.accounts.capital"))).toHaveValue("");
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.name")), "Cuenta D");
    expect(within(dialog).getByRole("button", { name: t("desks.accounts.newAccount.save") })).toBeDisabled();
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
    const calls = stubApi({ desks: [desk({})], accounts: [account({ desk_ids: [1] }), account({ id: 2, name: "Cuenta B" })] });
    renderShell();
    await openBroker();
    const buttons = await screen.findAllByRole("button", { name: t("desks.accounts.unassign", { desk: "Swing" }) });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]!);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Swing");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.unassign.confirm") }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/desks/1/accounts/1")).toBe(true));
    await waitFor(() => expect(screen.queryByRole("button", { name: t("desks.accounts.unassign", { desk: "Swing" }) })).toBeNull());
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
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.add.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toMatchObject({ url: "/api/desks/7/accounts", body: { account_id: 1 } }));
  });
});

const SIM = { id: 1, kind: "sim", name: "Hokma Simulated", commission: { type: "bps", value: 1, min: 0 }, slippage_bps: 2, capabilities: { accounts: true, reset: true, orders: true, positions: true }, orders_enabled: false };
const schwabBroker = (patch: Record<string, unknown> = {}) => ({
  id: 2, kind: "schwab", name: "Schwab", commission: { type: "bps", value: 0, min: 0 }, slippage_bps: 0,
  capabilities: { accounts: true, reset: false, orders: true, positions: true }, orders_enabled: false, ...patch,
});
const schwabAccount = (patch: Partial<Account> = {}) => account({ id: 9, broker_id: 2, broker: "Schwab", name: "Schwab ••••1234", mask: "••••1234", can_reset: false, ...patch });

describe("Schwab in the Broker tab (SPEC-015 18.2)", () => {
  it("shows the Schwab card with the orders switch off and one line of risk under it", async () => {
    stubApi({ desks: [], accounts: [account({}), schwabAccount()], brokers: [SIM, schwabBroker()] });
    renderShell();
    await openBroker();
    const heading = await screen.findByRole("heading", { name: /Schwab/ });
    const card = heading.closest(".brk-one") as HTMLElement;
    const toggle = within(card).getByRole("switch", { name: t("desks.broker.schwab.ordersToggle") });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    const risk = within(card).getByTestId("orders-risk");
    expect(risk).toHaveTextContent(t("desks.broker.schwab.ordersRisk"));
    expect(risk.textContent).not.toContain("\n");
    // The internal broker has no such switch.
    const sim = screen.getByRole("heading", { name: /Hokma Simulated/ }).closest(".brk-one") as HTMLElement;
    expect(within(sim).queryByRole("switch")).toBeNull();
    expect(screen.getAllByRole("switch")).toHaveLength(1);
  });

  it("turning the switch on calls the route with enabled true and the line of View account becomes the trading one", async () => {
    const calls = stubApi({ desks: [], accounts: [schwabAccount()], brokers: [SIM, schwabBroker()] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("switch", { name: t("desks.broker.schwab.ordersToggle") }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT" && c.url === "/api/brokers/2/orders-enabled")?.body).toEqual({ enabled: true }));
    await waitFor(() => expect(screen.getByRole("switch", { name: t("desks.broker.schwab.ordersToggle") })).toHaveAttribute("aria-checked", "true"));
    cleanup();
    renderWithApp(<SchwabAccount />);
    expect(await screen.findByTestId("schwab-readonly")).toHaveTextContent(t("connections.schwab.trading"));
  });

  it("with the switch off, View account shows the line of orders off", async () => {
    stubApi({ desks: [], accounts: [schwabAccount()], brokers: [SIM, schwabBroker()] });
    renderWithApp(<SchwabAccount />);
    expect(await screen.findByTestId("schwab-readonly")).toHaveTextContent(t("connections.schwab.readOnly"));
  });

  it("turns it off again and shows the server's message when the change fails", async () => {
    const calls = stubApi({ desks: [], accounts: [schwabAccount()], brokers: [SIM, schwabBroker({ orders_enabled: true })] });
    renderShell();
    await openBroker();
    const toggle = await screen.findByRole("switch", { name: t("desks.broker.schwab.ordersToggle") });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await userEvent.click(toggle);
    await waitFor(() => expect(calls.find((c) => c.method === "PUT" && c.url === "/api/brokers/2/orders-enabled")?.body).toEqual({ enabled: false }));
    await waitFor(() => expect(screen.getByRole("switch", { name: t("desks.broker.schwab.ordersToggle") })).toHaveAttribute("aria-checked", "false"));
  });

  it("Schwab accounts show their mask and have no Reiniciar; the internal ones keep it", async () => {
    stubApi({ desks: [desk({})], accounts: [account({}), schwabAccount()], brokers: [SIM, schwabBroker()] });
    renderShell();
    await openBroker();
    const row = await screen.findByRole("row", { name: /Schwab ••••1234/ });
    expect(row).toHaveTextContent("••••1234");
    expect(within(row).queryByRole("button", { name: t("desks.accounts.reset") })).toBeNull();
    expect(within(screen.getByRole("row", { name: /Cuenta A/ })).getByRole("button", { name: t("desks.accounts.reset") })).toBeInTheDocument();
  });

  it("'Asignar a mesa' works the same for a Schwab account", async () => {
    const calls = stubApi({ desks: [desk({ id: 7 })], accounts: [schwabAccount()], brokers: [SIM, schwabBroker()] });
    renderShell();
    await openBroker();
    const row = await screen.findByRole("row", { name: /Schwab ••••1234/ });
    await userEvent.click(within(row).getByRole("button", { name: t("desks.accounts.assign") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.add.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/desks/7/accounts")?.body).toEqual({ account_id: 9 }));
  });

  it("new accounts are offered only for the internal broker", async () => {
    stubApi({ desks: [], accounts: [account({})], brokers: [schwabBroker(), SIM] });
    renderShell();
    await openBroker();
    await userEvent.click(await screen.findByRole("button", { name: t("desks.accounts.newAccount") }));
    const dialog = await screen.findByRole("dialog", { name: t("desks.accounts.newAccount.title") });
    expect(within(dialog).queryByRole("option", { name: "Schwab" })).toBeNull();
    expect(within(dialog).getByRole("option", { name: "Hokma Simulated" })).toBeInTheDocument();
  });

  it("the texts of orders exist in es and en, with no dash and no 'simulated' wording", () => {
    for (const locale of ["es", "en"] as const) {
      const catalog = getCatalog(locale);
      for (const key of ["desks.broker.schwab.ordersToggle", "desks.broker.schwab.ordersRisk", "desks.broker.externalText2", "connections.schwab.trading", "connections.schwab.readOnly", "agents.kit.desk-chief.desc2", "permissions.firstrun.body2", "permissions.modal.body2", "protections.real_money2", "block.why.real_money2"]) {
        const text = catalog[key] ?? "";
        expect(text, `${locale} ${key}`).toBeTruthy();
        expect(text, `${locale} ${key}`).not.toContain(String.fromCharCode(0x2014));
        expect(text, `${locale} ${key}`).not.toMatch(/simulad|did.ctic|juguete|simulat|toy|play money/i);
      }
      for (const old of ["desks.broker.externalText", "permissions.firstrun.body", "permissions.modal.body", "protections.real_money", "block.why.real_money", "agents.kit.desk-chief.desc"]) {
        expect(catalog[old], `${locale} ${old} is retired`).toBeUndefined();
      }
    }
  });
});

describe("texts of the accounts of a desk (SPEC-027)", () => {
  it("name no provider outside Connections and carry no long dash, in es and en", () => {
    for (const locale of ["es", "en"] as const) {
      const catalog = getCatalog(locale);
      const keys = Object.keys(catalog).filter((k) => /^desks\.(accounts|card|broker|stat|kpi|fund|side|wizard|hokma|field)\./.test(k) && k !== "desks.fund.broker");
      for (const key of keys) {
        expect(catalog[key], `${locale} ${key}`).not.toMatch(/schwab|alpaca|ibkr|interactive brokers|tradovate|yahoo/i);
        expect(catalog[key], `${locale} ${key}`).not.toContain(String.fromCharCode(0x2014));
      }
      for (const key of ["desks.card.analysisOnly", "desks.card.add", "desks.accounts.shared.one", "desks.accounts.shared.many", "desks.accounts.add.firstNote"]) expect(catalog[key], `${locale} ${key}`).toBeTruthy();
    }
  });
});

describe("accounts in the desk bar (SPEC-027)", () => {
  it("lists every account with its balance, marks the shared one, and always offers 'Agregar cuenta'", async () => {
    stubApi({ desks: [], accounts: [] });
    const mine = desk({
      id: 3, capital: null,
      accounts: [
        { id: 4, name: "Cuenta A", broker: "Hokma Simulated", initial_capital: 25000, balance: { cash: 1, equity: 25000, currency: "USD" }, as_of: "", shared_with: [8, 9], strategies: ["principal"] },
        { id: 5, name: "Cuenta B", broker: "Hokma Simulated", initial_capital: 1000, balance: { cash: null, equity: null, currency: "USD" }, as_of: "", shared_with: [], strategies: [] },
      ],
    });
    renderWithApp(<DeskBar desk={mine} desks={[mine]} order={[...DESK_TABS]} tab="terminal" tabKey="k" onTab={() => undefined} onOrder={() => undefined} onAddPanel={() => undefined} onRemovePanel={() => undefined} onPickDesk={() => undefined} />);
    const bar = await screen.findByRole("banner");
    expect(bar).toHaveTextContent("Cuenta A");
    expect(bar).toHaveTextContent(t("desks.accounts.shared.many", { n: "2" }));
    expect(bar).toHaveTextContent("Cuenta B");
    expect(bar).toHaveTextContent(t("desks.accounts.unavailable"));
    expect(within(bar).getByRole("button", { name: t("desks.card.add") })).toBeInTheDocument();
    cleanup();
    const empty = desk({ id: 3, capital: null, accounts: [] });
    renderWithApp(<DeskBar desk={empty} desks={[empty]} order={[...DESK_TABS]} tab="terminal" tabKey="k" onTab={() => undefined} onOrder={() => undefined} onAddPanel={() => undefined} onRemovePanel={() => undefined} onPickDesk={() => undefined} />);
    expect(await screen.findByText(t("desks.card.analysisOnly"))).toBeInTheDocument();
  });
});

describe("accounts in the desk card (SPEC-027)", () => {
  const openFundCards = async () => {
    await userEvent.click(within(screen.getByRole("complementary", { name: t("nav.menu") })).getByRole("button", { name: /Mesas de dinero/ }));
    return screen.findByRole("button", { name: "Abrir la mesa Swing" });
  };
  const deskAccount = (patch: Record<string, unknown> = {}) => ({
    id: 4, name: "Cuenta A", broker: "Hokma Simulated", initial_capital: 25000, balance: { cash: 25000, equity: 25000, currency: "USD" }, as_of: "2026-11-18T00:00:00Z",
    shared_with: [], strategies: [], ...patch,
  });

  it("a desk without accounts says analysis only, never an empty or zero balance", async () => {
    stubApi({ desks: [desk({ id: 3, capital: null })], accounts: [] });
    renderShell();
    const card = await openFundCards();
    expect(card).toHaveTextContent(t("desks.card.analysisOnly"));
    expect(card).toHaveTextContent(t("desks.card.noAccount"));
    expect(card).not.toHaveTextContent(t("desks.accounts.balance"));
  });

  it("adds a NEW demo account with the typed name and capital, and the first one says the main strategy will use it", async () => {
    const calls = stubApi({ desks: [desk({ id: 3, capital: null })], accounts: [] });
    renderShell();
    await openFundCards();
    await userEvent.click(screen.getByRole("button", { name: t("desks.card.add") }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(t("desks.accounts.add.firstNote"))).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.name")), "Demo NQ");
    await userEvent.type(within(dialog).getByLabelText(t("desks.accounts.capital")), "5000");
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.add.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/desks/3/accounts")?.body).toEqual({ demo: { name: "Demo NQ", initial_capital: 5000 } }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Abrir la mesa Swing" })).toHaveTextContent(/Demo NQ\s*Hokma Simulated/));
  });

  it("refuses an empty name or a capital out of limits without calling the server", async () => {
    const calls = stubApi({ desks: [desk({ id: 3 })], accounts: [] });
    renderShell();
    await openFundCards();
    await userEvent.click(screen.getByRole("button", { name: t("desks.card.add") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("button", { name: t("desks.accounts.add.save") }));
    expect(await within(dialog).findByText(t("desks.wizard.required"))).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("adds an existing account that other desks already use and says which ones", async () => {
    const calls = stubApi({ desks: [desk({ id: 3 }), desk({ id: 8, name: "Opciones" })], accounts: [account({ id: 4, desk_ids: [8] })] });
    renderShell();
    await openFundCards();
    await userEvent.click(screen.getAllByRole("button", { name: t("desks.card.add") })[0]!);
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("radio", { name: new RegExp(t("desks.accounts.add.existing")) }));
    expect(within(dialog).getByRole("option", { name: new RegExp(`Cuenta A.*${t("desks.accounts.add.alsoIn", { desks: "Opciones" })}`) })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: t("desks.accounts.add.save") }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/desks/3/accounts")?.body).toEqual({ account_id: 4 }));
  });

  it("marks an account shared with other desks and shows the balance of each account", async () => {
    stubApi({ desks: [desk({ id: 3, accounts: [deskAccount({ shared_with: [8] }), deskAccount({ id: 5, name: "Cuenta B", balance: { cash: null, equity: null, currency: "USD" } })] })], accounts: [] });
    renderShell();
    const card = await openFundCards();
    expect(card).toHaveTextContent(t("desks.accounts.shared.one"));
    expect(card).toHaveTextContent("Cuenta B");
    expect(card).toHaveTextContent(t("desks.accounts.unavailable"));
  });

  it("an account already in every desk offers no desk, and says so", async () => {
    stubApi({ desks: [desk({ id: 3, accounts: [deskAccount()] })], accounts: [account({ id: 4, desk_ids: [3] })] });
    renderShell();
    await openFundCards();
    await userEvent.click(screen.getByRole("button", { name: t("desks.card.add") }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByRole("radio", { name: new RegExp(t("desks.accounts.add.existing")) }));
    expect(await within(dialog).findByText(t("desks.accounts.add.noAccounts"))).toBeInTheDocument();
  });
});
