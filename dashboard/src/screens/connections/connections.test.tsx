import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { BAD_KEY_TEST, FakeConnections, SECRET, apiError } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { HashRoute } from "./HashRoute";
import { getFocus, parseConnectionsHash, requestFocus } from "./focus";

let backend: FakeConnections;

function Probe() {
  const shell = useShell();
  return <output aria-label="tabs">{shell.tabs.map((tab) => tab.screen).join("|")}</output>;
}

async function show() {
  const view = renderWithApp(
    <>
      <ConnectionsScreen />
      <Probe />
    </>,
  );
  await screen.findByTestId("connection-coinbase");
  return view;
}

const rowOf = (id: string) => screen.getByTestId("connection-" + id);
const connectedIds = () => [...screen.getByTestId("connected-list").querySelectorAll("li[data-provider]")].map((li) => li.getAttribute("data-provider"));
const addButton = () => screen.getByRole("button", { name: t("connections.add") });

async function openAdd() {
  await userEvent.click(addButton());
  return screen.findByRole("dialog", { name: t("connections.add.title") });
}

async function pick(name: string) {
  const list = await openAdd();
  await userEvent.click(within(list).getByRole("button", { name: t("connections.add.choose"), description: name }));
}

async function openMenu(name: string) {
  await userEvent.click(screen.getByRole("button", { name: t("connections.row.menu", { name }) }));
}

/** Connects a provider so a test can start from a screen that has it in "Connected". */
function connect(id: string, over: Record<string, unknown> = {}) {
  Object.assign(backend.find(id), { added: true, key_state: "saved", state: "connected", ...over });
}

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

describe("the screen at the start", () => {
  it("has one action button, Add, and no group cards nor a Priority section", async () => {
    await show();
    expect(screen.getByRole("heading", { name: t("connections.title") })).toBeInTheDocument();
    expect(screen.getByText(t("connections.intro2"))).toBeInTheDocument();
    expect(addButton()).toBeEnabled();
    const actions = [...document.querySelectorAll(".conn-header > button")];
    expect(actions).toHaveLength(1);
    expect(document.querySelector("[data-group]")).toBeNull();
    expect(document.querySelector("article[data-provider]")).toBeNull();
    expect(screen.queryByText("Priority")).toBeNull();
    expect(screen.queryByText("Prioridad")).toBeNull();
  });

  it("lists Coinbase, which needs no key and is on, and Yahoo Finance fixed at the end", async () => {
    await show();
    expect(connectedIds()).toEqual(["coinbase", "yahoo"]);
    expect(within(rowOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(within(rowOf("coinbase")).getByTestId("conn-data")).toHaveTextContent(t("connections.data.live"));
    expect(within(rowOf("coinbase")).getByTestId("conn-detail")).toHaveTextContent(t("connections.add.what.coinbase"));
    expect(screen.getByTestId("connected-empty")).toHaveTextContent(t("connections.connected.empty"));
  });

  it("Yahoo Finance has no menu, no switch and says it is the fallback and delayed", async () => {
    await show();
    const yahoo = rowOf("yahoo");
    expect(yahoo).toHaveTextContent("Yahoo Finance");
    expect(yahoo).toHaveTextContent(t("connections.fallback.yahoo"));
    expect(yahoo).toHaveTextContent(t("connections.data.delayed"));
    expect(within(yahoo).queryByRole("button")).toBeNull();
    expect(within(yahoo).queryByRole("switch")).toBeNull();
  });

  it("Order of use shows only the uses that have a connected source, with only the connected ones", async () => {
    await show();
    const order = screen.getByTestId("use-order");
    expect(within(order).getByText(t("connections.order.help"))).toBeInTheDocument();
    const rows = [...order.querySelectorAll("li.conn-use")].map((li) => li.getAttribute("data-testid"));
    expect(rows).toEqual(["use-crypto"]);
    expect(within(screen.getByTestId("use-items-crypto")).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByTestId("use-items-crypto")).toHaveTextContent("1 Coinbase");
    expect(screen.queryByRole("button", { name: t("connections.order.change") })).toBeNull(); // one source: nothing to order
  });

  it("is a section of the main navigation", async () => {
    const { SIDEBAR_SCREENS, BUILTIN_SCREENS } = await import("@/shell/screens");
    expect(SIDEBAR_SCREENS).toContain("connections");
    expect(BUILTIN_SCREENS.connections!.navKey).toBe("nav.connections");
    expect(t("nav.connections")).toBe("Connections");
    act(() => setLocale("es"));
    expect(t("nav.connections")).toBe("Conexiones");
  });
});

describe("the Add dialog", () => {
  it("lists what can be connected with what each gives, and says Already connected for Coinbase", async () => {
    await show();
    const list = await openAdd();
    const names = [...list.querySelectorAll("[data-testid^='add-'] .h2")].map((e) => e.textContent);
    expect(names).toEqual(["Schwab", "Alpaca Trading", "Finnhub", "SiomTrading Futures", t("mail.title"), "Coinbase"]);
    for (const id of ["schwab", "alpaca", "finnhub", "siomtrading_futures", "mail", "coinbase"]) {
      expect(within(screen.getByTestId("add-" + id)).getByText(t("connections.add.what." + id))).toBeInTheDocument();
    }
    expect(within(screen.getByTestId("add-coinbase")).getByText(t("connections.add.already"))).toBeInTheDocument();
    expect(within(screen.getByTestId("add-coinbase")).queryByRole("button")).toBeNull();
    expect(within(list).getAllByRole("button", { name: t("connections.add.choose") })).toHaveLength(5);
    expect(within(list).getByText(t("connections.add.fallbackNote"))).toBeInTheDocument();
    expect(within(list).queryByText(/^Yahoo Finance$/)).toBeNull(); // Yahoo is not in the list
    expect(screen.getByTestId("add-list").textContent).not.toMatch(/gmail\.com/);
  });

  it("closes with Escape and with the close button, and gives the focus back to Add", async () => {
    await show();
    await openAdd();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await openAdd();
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.close") }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(addButton()).toHaveFocus();
  });

  it("every control has an accessible name", async () => {
    await show();
    const list = await openAdd();
    for (const button of within(list).getAllByRole("button")) expect(button).toHaveAccessibleName();
  });

  it("the form shows the fields of key_fields: Finnhub one, Alpaca two, Coinbase none", async () => {
    await show();
    await pick("Finnhub");
    const finnhubForm = screen.getByRole("form", { name: t("connections.add.formTitle", { name: "Finnhub" }) });
    expect(finnhubForm.querySelectorAll("input")).toHaveLength(1);
    expect(within(finnhubForm).getByLabelText(t("connections.field.key"))).toHaveAttribute("type", "password");
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.back") }));
    await userEvent.click(within(screen.getByTestId("add-list")).getByRole("button", { name: t("connections.add.choose"), description: "Alpaca Trading" }));
    const alpacaForm = screen.getByRole("form", { name: t("connections.add.formTitle", { name: "Alpaca Trading" }) });
    expect(alpacaForm.querySelectorAll("input")).toHaveLength(2);
    expect(within(alpacaForm).getByLabelText(t("connections.field.keyId"))).toBeInTheDocument();
    expect(within(alpacaForm).getByLabelText(t("connections.field.secret"))).toBeInTheDocument();
    expect(within(alpacaForm).getByRole("button", { name: t("connections.add.testAndAdd") })).toBeDisabled();
  });

  it("SiomTrading Futures asks for its access token", async () => {
    await show();
    await pick("SiomTrading Futures");
    expect(screen.getByLabelText(t("connections.field.token"))).toBeInTheDocument();
  });

  it("Test and add keeps a good key: the row appears, the field is empty and the DOM never has the key", async () => {
    await show();
    await pick("Finnhub");
    await userEvent.type(screen.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    expect(await screen.findByTestId("add-done")).toHaveTextContent(t("connections.add.added", { name: "Finnhub" }));
    expect(backend.sent("POST", "/finnhub/add")[0]!.body).toEqual({ key: SECRET });
    expect(backend.sent("PUT", "/finnhub/key")).toHaveLength(0); // one route does the three steps
    expect(document.body.innerHTML).not.toContain(SECRET);
    for (const input of document.querySelectorAll("input")) expect((input as HTMLInputElement).value).toBe("");
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.done") }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(connectedIds()).toContain("finnhub"));
    expect(within(rowOf("finnhub")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(screen.getByTestId("use-items-equity")).toHaveTextContent("Finnhub");
  });

  it("Alpaca sends Key ID and Secret together and keeps neither", async () => {
    await show();
    await pick("Alpaca Trading");
    await userEvent.type(screen.getByLabelText(t("connections.field.keyId")), "AKID-1234567890");
    await userEvent.type(screen.getByLabelText(t("connections.field.secret")), SECRET);
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    await screen.findByTestId("add-done");
    expect(backend.sent("POST", "/alpaca/add")[0]!.body).toEqual({ key_id: "AKID-1234567890", secret: SECRET });
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain("AKID-1234567890");
  });

  it("a failed test shows Not added with the reason and adds no row", async () => {
    backend.failAdd.set("finnhub", BAD_KEY_TEST);
    await show();
    await pick("Finnhub");
    await userEvent.type(screen.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    const error = await screen.findByTestId("add-error");
    expect(error).toHaveTextContent(t("connections.add.failed", { reason: t("connections.err.auth_failed") }));
    expect(error).toHaveTextContent("Not added: The provider did not accept the key. Check it and try again.");
    expect(backend.store.has("finnhub")).toBe(false);
    expect(screen.queryByTestId("connection-finnhub")).toBeNull();
    expect(connectedIds()).toEqual(["coinbase", "yahoo"]);
    // the dialog stays open on the same form so the student can fix the key
    expect(screen.getByLabelText(t("connections.field.key"))).toBeInTheDocument();
  });

  it("Coinbase has no field: Test and add sends an empty body and turns it on", async () => {
    Object.assign(backend.find("coinbase"), { added: false, enabled: false, state: "not_connected" });
    await show().catch(() => undefined);
    await userEvent.click(addButton());
    const list = await screen.findByRole("dialog", { name: t("connections.add.title") });
    await userEvent.click(within(list).getByRole("button", { name: t("connections.add.choose"), description: "Coinbase" }));
    const form = screen.getByRole("form", { name: t("connections.add.formTitle", { name: "Coinbase" }) });
    expect(form.querySelectorAll("input")).toHaveLength(0);
    await userEvent.click(within(form).getByRole("button", { name: t("connections.add.testAndAdd") }));
    await screen.findByTestId("add-done");
    expect(backend.sent("POST", "/coinbase/add")[0]!.body).toEqual({});
  });

  it("without a credential store it says so, and the answer 503 shows the banner", async () => {
    await show();
    backend.storeAvailable = false;
    await pick("Finnhub");
    await userEvent.type(screen.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    expect(await screen.findByTestId("store-unavailable")).toHaveTextContent("System credential store not available");
    expect(screen.getByTestId("add-error")).toHaveTextContent(t("connections.err.store_unavailable"));
    expect(backend.store.size).toBe(0);
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("the banner of the credential store shows from the first load", async () => {
    backend.storeAvailable = false;
    await show();
    expect(screen.getByTestId("store-unavailable")).toHaveTextContent("System credential store not available");
  });

  it("Gmail goes inside the dialog with its own card, and a connected Gmail has its row", async () => {
    await show();
    await pick(t("mail.title"));
    expect(screen.getByTestId("mail-card")).toBeInTheDocument();
    expect(screen.getByLabelText(t("mail.field.email"))).toBeInTheDocument();
    expect(connectedIds()).not.toContain("mail");
  });

  it("a connected Gmail shows as Already connected and has a row with its address", async () => {
    backend.mail = { ...backend.mail, connected: true, email: "ana@example.test" };
    await show();
    await waitFor(() => expect(connectedIds()).toContain("mail"));
    expect(within(rowOf("mail")).getByTestId("conn-detail")).toHaveTextContent("ana@example.test");
    expect(within(rowOf("mail")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    await openAdd();
    expect(within(screen.getByTestId("add-mail")).getByText(t("connections.add.already"))).toBeInTheDocument();
    expect(connectedIds().at(-1)).toBe("yahoo");
  });
});

describe("the connected rows", () => {
  beforeEach(() => {
    connect("alpaca");
    connect("finnhub", { data_state: "delayed" });
    connect("siomtrading_futures", { state: "reconnecting" });
  });

  it("each row says its state and its kind of data, Yahoo stays last, Coinbase first by the order of the list", async () => {
    await show();
    expect(connectedIds().at(-1)).toBe("yahoo");
    expect(connectedIds()).toEqual(["alpaca", "finnhub", "siomtrading_futures", "coinbase", "yahoo"]);
    expect(within(rowOf("alpaca")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(within(rowOf("finnhub")).getByTestId("conn-data")).toHaveTextContent(t("connections.data.delayed"));
    expect(within(rowOf("siomtrading_futures")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.reconnecting"));
    expect(screen.queryByTestId("connected-empty")).toBeNull();
  });

  it("an error row shows the catalog text of its code, in the active language", async () => {
    Object.assign(backend.find("alpaca"), { state: "error", error_code: "connections.auth_failed", error_message: "server text" });
    await show();
    expect(within(rowOf("alpaca")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.error"));
    const english = t("connections.err.auth_failed");
    expect(within(rowOf("alpaca")).getByTestId("conn-error")).toHaveTextContent(english);
    act(() => setLocale("es"));
    expect(t("connections.err.auth_failed")).not.toBe(english);
    expect(within(rowOf("alpaca")).getByTestId("conn-error")).toHaveTextContent(t("connections.err.auth_failed"));
  });

  it("an unknown code falls back to the text the server sent", async () => {
    Object.assign(backend.find("alpaca"), { state: "error", error_code: "connections.brand_new", error_message: "Server wording" });
    await show();
    expect(within(rowOf("alpaca")).getByTestId("conn-error")).toHaveTextContent("Server wording");
  });

  it("the menu has Test connection, Pause, Change key, View guide and Remove", async () => {
    await show();
    await openMenu("Finnhub");
    const items = screen.getAllByRole("menuitem").map((item) => item.textContent);
    expect(items).toEqual([t("connections.test"), t("connections.row.pause"), t("connections.row.changeKey"), t("connections.row.guide"), t("connections.row.remove")]);
  });

  it("Coinbase has no Change key in its menu: it has no key", async () => {
    await show();
    await openMenu("Coinbase");
    expect(screen.queryByRole("menuitem", { name: t("connections.row.changeKey") })).toBeNull();
    expect(screen.getByRole("menuitem", { name: t("connections.row.guide") })).toBeInTheDocument();
  });

  it("Test connection shows Connected, the kind of data and the latency", async () => {
    await show();
    await openMenu("Coinbase");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.test") }));
    const result = await within(rowOf("coinbase")).findByTestId("conn-test-result");
    expect(result).toHaveAttribute("data-ok", "true");
    expect(result).toHaveTextContent(t("connections.test.ok", { data: t("connections.data.live"), ms: 240 }));
  });

  it("Test connection shows the catalog error when it fails", async () => {
    backend.testAnswer = BAD_KEY_TEST;
    await show();
    await openMenu("Alpaca Trading");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.test") }));
    const result = await within(rowOf("alpaca")).findByTestId("conn-test-result");
    expect(result).toHaveAttribute("data-ok", "false");
    expect(result).toHaveTextContent(t("connections.test.fail", { reason: t("connections.err.auth_failed") }));
  });

  it("Pause turns the provider off and the row says Paused; Turn on brings it back", async () => {
    await show();
    await openMenu("Finnhub");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.pause") }));
    await waitFor(() => expect(within(rowOf("finnhub")).getByTestId("conn-state")).toHaveTextContent(t("connections.row.paused")));
    expect(backend.sent("PUT", "/finnhub/enabled")[0]!.body).toEqual({ enabled: false });
    expect(connectedIds()).toContain("finnhub"); // paused stays in the list
    await openMenu("Finnhub");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.resume") }));
    await waitFor(() => expect(within(rowOf("finnhub")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected")));
    expect(backend.sent("PUT", "/finnhub/enabled")[1]!.body).toEqual({ enabled: true });
  });

  it("Change key opens the form of that provider and sends the new key to add", async () => {
    await show();
    await openMenu("Finnhub");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.changeKey") }));
    const open = await screen.findByRole("dialog");
    await userEvent.type(within(open).getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(within(open).getByRole("button", { name: t("connections.add.testAndAdd") }));
    await screen.findByTestId("add-done");
    expect(backend.sent("POST", "/finnhub/add")[0]!.body).toEqual({ key: SECRET });
  });

  it("View guide opens the guide in the dialog, without fields", async () => {
    await show();
    await openMenu("Alpaca Trading");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.guide") }));
    const open = await screen.findByRole("dialog");
    expect(within(open).getByTestId("guide-what")).toHaveTextContent(t("connections.guide.alpaca.what"));
    expect(open.querySelector("input")).toBeNull();
  });

  it("Remove asks first: Cancel keeps the row, Remove sends the removal and the row leaves", async () => {
    await show();
    await openMenu("Finnhub");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.remove") }));
    const ask = await screen.findByRole("alertdialog");
    expect(within(ask).getByText(t("connections.row.removeConfirm", { name: "Finnhub" }))).toBeInTheDocument();
    await userEvent.click(within(ask).getByRole("button", { name: t("connections.row.cancel") }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(backend.sent("POST", "/finnhub/remove")).toHaveLength(0);
    expect(connectedIds()).toContain("finnhub");

    await openMenu("Finnhub");
    await userEvent.click(screen.getByRole("menuitem", { name: t("connections.row.remove") }));
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: t("connections.row.remove") }));
    await waitFor(() => expect(connectedIds()).not.toContain("finnhub"));
    expect(backend.sent("POST", "/finnhub/remove")[0]!.body).toEqual({});
  });
});

describe("Order of use", () => {
  beforeEach(() => {
    connect("alpaca");
    connect("finnhub");
  });
  const items = (use: string) => [...screen.getByTestId("use-items-" + use).querySelectorAll("li")].map((li) => li.getAttribute("data-provider"));

  it("a row per use with connected sources, only the connected ones inside, Change only with two or more", async () => {
    await show();
    const rows = [...screen.getByTestId("use-order").querySelectorAll("li.conn-use")].map((li) => li.getAttribute("data-testid"));
    // Futures has SiomTrading Futures, which is not connected: no row. Schwab is not connected: it is not in any row.
    expect(rows).toEqual(["use-equity", "use-bars", "use-options", "use-crypto", "use-forex", "use-news"]);
    expect(items("equity")).toEqual(["alpaca", "finnhub"]);
    expect(items("options")).toEqual(["alpaca"]);
    expect(items("crypto")).toEqual(["coinbase", "finnhub"]);
    const withChange = screen.getAllByRole("button", { name: t("connections.order.change") });
    expect(withChange).toHaveLength(4); // equity, bars, crypto, news
    expect(within(screen.getByTestId("use-options")).queryByRole("button")).toBeNull();
    expect(within(screen.getByTestId("use-forex")).queryByRole("button")).toBeNull();
  });

  it("Change opens the row as a list with arrows and Done closes it", async () => {
    await show();
    await userEvent.click(within(screen.getByTestId("use-crypto")).getByRole("button", { name: t("connections.order.change") }));
    const list = screen.getByTestId("priority-crypto");
    expect(within(list).getAllByRole("listitem").map((li) => li.getAttribute("data-provider"))).toEqual(["coinbase", "finnhub"]);
    expect(within(list).getAllByRole("button", { name: /^Move/ })[0]).toBeDisabled();
    await userEvent.click(within(screen.getByTestId("use-crypto")).getByRole("button", { name: t("connections.order.done") }));
    expect(screen.queryByTestId("priority-crypto")).toBeNull();
  });

  it("the arrow buttons send the whole order: the connected ones in the new order and the others behind", async () => {
    await show();
    await userEvent.click(within(screen.getByTestId("use-equity")).getByRole("button", { name: t("connections.order.change") }));
    await userEvent.click(within(screen.getByTestId("priority-equity")).getByRole("button", { name: t("connections.priority.down", { name: "Alpaca Trading" }) }));
    await waitFor(() => expect(backend.sent("PUT", "/connections/priority")).toHaveLength(1));
    // Schwab is not connected: it stays where it was, behind the connected ones
    expect(backend.sent("PUT", "/connections/priority")[0]!.body).toEqual({ asset_class: "equity", order: ["finnhub", "alpaca", "schwab"] });
    await waitFor(() => expect(within(screen.getByTestId("priority-equity")).getAllByRole("listitem").map((li) => li.getAttribute("data-provider"))).toEqual(["finnhub", "alpaca"]));
  });

  it("dragging a row onto another reorders and sends it", async () => {
    await show();
    await userEvent.click(within(screen.getByTestId("use-crypto")).getByRole("button", { name: t("connections.order.change") }));
    const rows = screen.getByTestId("priority-crypto").querySelectorAll("li");
    const data = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(rows[1]!, { dataTransfer: data });
    fireEvent.dragOver(rows[0]!, { dataTransfer: data });
    fireEvent.drop(rows[0]!, { dataTransfer: data });
    await waitFor(() => expect(backend.sent("PUT", "/connections/priority")).toHaveLength(1));
    expect(backend.sent("PUT", "/connections/priority")[0]!.body).toEqual({ asset_class: "crypto", order: ["finnhub", "coinbase"] });
  });

  it("puts the order back and says why when the engine refuses it", async () => {
    backend.priorityFails = true;
    await show();
    await userEvent.click(within(screen.getByTestId("use-crypto")).getByRole("button", { name: t("connections.order.change") }));
    await userEvent.click(within(screen.getByTestId("priority-crypto")).getByRole("button", { name: t("connections.priority.down", { name: "Coinbase" }) }));
    expect(await screen.findByText(t("connections.err.bad_priority"))).toBeInTheDocument();
    expect(within(screen.getByTestId("priority-crypto")).getAllByRole("listitem").map((li) => li.getAttribute("data-provider"))).toEqual(["coinbase", "finnhub"]);
  });

  it("a connected Schwab goes first in the rows it serves", async () => {
    connect("schwab", { session: { state: "signed_in", session_expires_at: Math.floor(Date.now() / 1000) + 5 * 86400, accounts: [{ account_ref: "A1", mask: "••••1234" }] } });
    await show();
    expect(items("equity")).toEqual(["schwab", "alpaca", "finnhub"]);
    expect(items("bars")).toEqual(["schwab", "alpaca", "finnhub"]);
    expect(items("options")).toEqual(["schwab", "alpaca"]);
  });
});

describe("the state follows the engine without a reload", () => {
  const status = (provider: string, state: string, extra: Record<string, unknown> = {}) => ({
    type: "connection_status", provider, source: provider, epoch: "e1", state, data_state: "live", error_code: null, enabled: true, key_state: "not_required", symbols_in_use: 0, ...extra,
  });

  it("goes Connected, Reconnecting, Connected as connection_status arrives, with no new request", async () => {
    await show();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    const pill = within(rowOf("coinbase")).getByTestId("conn-state");
    expect(pill).toHaveTextContent(t("connections.state.connected"));
    const requestsBefore = backend.calls.length;
    act(() => socket.serverSend(status("coinbase", "reconnecting")));
    expect(within(rowOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.reconnecting"));
    act(() => socket.serverSend(status("coinbase", "connected")));
    expect(within(rowOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(backend.calls.length).toBe(requestsBefore);
  });

  it("an error from the engine shows its catalog text", async () => {
    connect("alpaca");
    await show();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    act(() => socket.serverSend(status("alpaca", "error", { error_code: "connections.connection_limit", key_state: "saved" })));
    expect(within(rowOf("alpaca")).getByTestId("conn-error")).toHaveTextContent(t("connections.err.connection_limit"));
  });

  it("opens the channel without subscribing to any symbol, and pings", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      await show();
      const socket = FakeSocket.last();
      act(() => socket.serverOpen());
      expect(socket.ops("subscribe")).toEqual([]);
      act(() => {
        vi.advanceTimersByTime(15_000);
      });
      expect(socket.ops("ping")).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("when the channel opens again the list is loaded again, so nothing is missed", async () => {
    await show();
    const socket = FakeSocket.last();
    act(() => socket.serverOpen());
    const lists = () => backend.calls.filter((c) => c.method === "GET" && c.path === "/api/connections").length;
    const before = lists();
    backend.find("coinbase").state = "error";
    backend.find("coinbase").error_code = "connections.unreachable";
    act(() => socket.serverClose(1011));
    await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(1), { timeout: 3000 });
    act(() => FakeSocket.last().serverOpen());
    await waitFor(() => expect(lists()).toBe(before + 1));
    await waitFor(() => expect(within(rowOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.error")));
  });
});

describe("what the chart link and the address bring forward", () => {
  it("reads #/connections, #/connections?class=equity and #/connections?focus=schwab&login=1", () => {
    expect(parseConnectionsHash("#/connections")).toEqual({ assetClass: null, provider: null, login: false });
    expect(parseConnectionsHash("#/connections?class=equity")).toEqual({ assetClass: "equity", provider: null, login: false });
    expect(parseConnectionsHash("#/connections?focus=schwab&login=1")).toEqual({ assetClass: null, provider: "schwab", login: true });
    expect(parseConnectionsHash("#/chart")).toBeNull();
    expect(parseConnectionsHash("")).toBeNull();
  });

  it("marks the connected rows of the class and the row of its use", async () => {
    connect("alpaca");
    connect("finnhub");
    requestFocus("equity");
    await show();
    const marked = [...document.querySelectorAll("[data-focus='true']")].map((e) => e.getAttribute("data-testid"));
    expect(marked.sort()).toEqual(["connection-alpaca", "connection-finnhub", "use-equity"]);
    expect(rowOf("coinbase")).not.toHaveAttribute("data-focus");
  });

  it("marks the row of a provider asked by name", async () => {
    connect("alpaca");
    requestFocus(null, { provider: "alpaca" });
    await show();
    expect([...document.querySelectorAll("[data-focus='true']")].map((e) => e.getAttribute("data-testid"))).toEqual(["connection-alpaca"]);
  });

  it("scrolls the marked row into view", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      connect("alpaca");
      requestFocus("equity");
      await show();
      await waitFor(() => expect(scroll).toHaveBeenCalled());
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it("a hash route opens the Connections tab, asks for the class and cleans the address", async () => {
    window.history.replaceState(null, "", "/#/connections?class=crypto");
    renderWithApp(
      <>
        <HashRoute />
        <Probe />
      </>,
    );
    await waitFor(() => expect(screen.getByLabelText("tabs")).toHaveTextContent("connections"));
    expect(getFocus().assetClass).toBe("crypto");
    expect(window.location.hash).toBe("");
    act(() => {
      window.history.replaceState(null, "", "/#/connections?focus=schwab&login=1");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(getFocus()).toMatchObject({ provider: "schwab", login: true, assetClass: null });
  });

  it("an unknown class is ignored, not a crash", async () => {
    requestFocus("bonds");
    await show();
    expect(document.querySelector("[data-focus='true']")).toBeNull();
  });
});

describe("load errors", () => {
  it("says it could not load and offers a retry", async () => {
    let fail = true;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => (fail ? Promise.resolve(apiError(502, "network", "Down")) : backend.handle(input, init))));
    renderWithApp(<ConnectionsScreen />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(t("connections.error.title"));
    fail = false;
    await userEvent.click(within(alert).getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByTestId("connection-coinbase")).toBeInTheDocument();
  });

});
