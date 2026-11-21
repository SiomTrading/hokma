import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { ACCOUNT_PANEL, FakeConnections } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { SchwabAccount } from "./SchwabAccount";

let backend: FakeConnections;

beforeEach(() => {
  backend = new FakeConnections();
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the View account panel", () => {
  it("shows Balances, Positions and Open orders of the account, masked", async () => {
    renderWithApp(<SchwabAccount />);
    const card = await screen.findByTestId("schwab-account-A1");
    expect(within(card).getByRole("heading", { name: "Account ••••1234" })).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: t("connections.schwab.balances") })).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: t("connections.schwab.positions") })).toBeInTheDocument();
    expect(within(card).getByRole("heading", { name: t("connections.schwab.openOrders") })).toBeInTheDocument();

    const balances = within(screen.getByTestId("schwab-balances"));
    expect(balances.getByText(t("connections.schwab.bal.equity")).nextElementSibling).toHaveTextContent("$125,000.50");
    expect(balances.getByText(t("connections.schwab.bal.buyingPower")).nextElementSibling).toHaveTextContent("$250,000.00");
    expect(balances.getByText(t("connections.schwab.bal.availableFunds")).nextElementSibling).toHaveTextContent("$98,000.25");
    expect(balances.getByText(t("connections.schwab.bal.startOfDay")).nextElementSibling).toHaveTextContent("$124,000.00");

    const positions = within(screen.getByTestId("schwab-positions"));
    expect(positions.getByRole("rowheader", { name: "AAPL" })).toBeInTheDocument();
    expect(positions.getByText("EQUITY")).toBeInTheDocument();
    expect(positions.getByText("$1,900.00")).toBeInTheDocument();
    expect(positions.getByText("+0.66%")).toBeInTheDocument();

    const orders = within(screen.getByTestId("schwab-orders"));
    expect(orders.getByRole("rowheader", { name: "MSFT" })).toBeInTheDocument();
    expect(orders.getByText("BUY")).toBeInTheDocument();
    expect(orders.getByText("LIMIT")).toBeInTheDocument();
    expect(orders.getByText("WORKING")).toBeInTheDocument();
    expect(orders.getByText("$400.25")).toBeInTheDocument();
  });

  it("with the orders switch off it says agents do not trade in the account, and has no button to send, change or cancel an order", async () => {
    renderWithApp(<SchwabAccount />);
    await screen.findByTestId("schwab-account-A1");
    expect(screen.getByTestId("schwab-readonly")).toHaveTextContent("Hokma reads your Schwab account. Agents do not trade in it while orders are off in the Broker tab.");
    expect(within(screen.getByTestId("schwab-account-panel")).queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByTestId("schwab-account-panel").textContent).not.toMatch(/\b(buy now|sell now|place order|cancel order|replace order)\b/i);
  });

  it("with the orders switch on it says the agents trade in it with real money, and still has no order button", async () => {
    backend.account = { ...ACCOUNT_PANEL, orders_enabled: true };
    renderWithApp(<SchwabAccount />);
    await screen.findByTestId("schwab-account-A1");
    expect(screen.getByTestId("schwab-readonly")).toHaveTextContent(t("connections.schwab.trading"));
    expect(screen.getByTestId("schwab-readonly")).toHaveTextContent("Hokma agents trade in this account with real money, following the rules of the desk that uses it.");
    expect(within(screen.getByTestId("schwab-account-panel")).queryAllByRole("button")).toHaveLength(0);
  });

  it("shows no hash and no number of account: only the account_ref's mask", async () => {
    renderWithApp(<SchwabAccount />);
    await screen.findByTestId("schwab-account-A1");
    expect(document.body.textContent).toContain("••••1234");
    expect(document.body.innerHTML).not.toMatch(/hashValue|accountNumber/);
  });

  it("a cash account shows its cash and what is available to trade", async () => {
    backend.account = { accounts: [{ account_ref: "A2", mask: "••••9876", type: "CASH", currentBalances: { totalCash: 5000, cashAvailableForTrading: 4500 }, initialBalances: { liquidationValue: 5100 }, positions: [], orders: [] }] };
    renderWithApp(<SchwabAccount />);
    const balances = within(await screen.findByTestId("schwab-balances"));
    expect(balances.getByText(t("connections.schwab.bal.totalCash")).nextElementSibling).toHaveTextContent("$5,000.00");
    expect(balances.getByText(t("connections.schwab.bal.cashAvailable")).nextElementSibling).toHaveTextContent("$4,500.00");
    expect(balances.queryByText(t("connections.schwab.bal.buyingPower"))).toBeNull();
    expect(within(screen.getByTestId("schwab-positions")).getByText(t("connections.schwab.none"))).toBeInTheDocument();
    expect(within(screen.getByTestId("schwab-orders")).getByText(t("connections.schwab.none"))).toBeInTheDocument();
  });

  it("a number that did not arrive is a dash, never zero", async () => {
    backend.account = { accounts: [{ account_ref: "A1", mask: "••••1234", type: "MARGIN", currentBalances: { equity: null }, positions: [], orders: [] }] };
    renderWithApp(<SchwabAccount />);
    const balances = within(await screen.findByTestId("schwab-balances"));
    expect(balances.getByText(t("connections.schwab.bal.equity")).nextElementSibling).toHaveTextContent("-");
    expect(balances.getByText(t("connections.schwab.bal.equity")).nextElementSibling).not.toHaveTextContent("$0");
  });

  it("orders that name their account go to it", async () => {
    backend.account = {
      accounts: [{ account_ref: "A1", mask: "••••1234", type: "MARGIN" }, { account_ref: "A2", mask: "••••9876", type: "MARGIN" }],
      orders: [{ account_ref: "A2", orderLegCollection: [{ instruction: "SELL", quantity: 1, instrument: { symbol: "TSLA" } }], status: "QUEUED" }],
    };
    renderWithApp(<SchwabAccount />);
    await screen.findByTestId("schwab-account-A2");
    expect(within(screen.getByTestId("schwab-account-A2")).getByRole("rowheader", { name: "TSLA" })).toBeInTheDocument();
    expect(within(screen.getByTestId("schwab-account-A1")).queryByRole("rowheader", { name: "TSLA" })).toBeNull();
  });

  it("shows the error of the engine with a retry", async () => {
    backend.account = { status: 409, code: "connections.no_login" };
    renderWithApp(<SchwabAccount />);
    expect(await screen.findByTestId("schwab-account-error")).toHaveTextContent(t("connections.err.no_login"));
    backend.account = ACCOUNT_PANEL;
    await userEvent.click(screen.getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByTestId("schwab-account-A1")).toBeInTheDocument();
  });

  it("opens from the menu of the Schwab row, in a dialog with a close button", async () => {
    Object.assign(backend.find("schwab"), { added: true, key_state: "saved", state: "connected", session: { state: "signed_in", session_expires_at: Math.floor(Date.now() / 1000) + 4 * 86400, accounts: [{ account_ref: "A1", mask: "••••1234" }] } });
    renderWithApp(<ConnectionsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: t("connections.row.menu", { name: "Schwab" }) }));
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.schwab.viewAccount") }));
    const dialog = await screen.findByRole("dialog", { name: t("connections.schwab.viewAccount") });
    await within(dialog).findByTestId("schwab-account-A1");
    expect(backend.sent("GET", "/schwab/account")).toHaveLength(1);
    await userEvent.click(within(dialog).getByRole("button", { name: t("connections.add.close") }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
