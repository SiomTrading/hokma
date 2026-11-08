import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { FakeSocket, installFakeSockets } from "@/test/fakeSocket";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ConnectionsScreen } from "./ConnectionsScreen";
import { HashRoute } from "./HashRoute";
import { getFocus, parseConnectionsHash, requestFocus } from "./focus";
import type { ConnectionCard, ConnectionsList } from "./types";

const SECRET = "sk-live-VERY-SECRET-value-98765";

function card(over: Partial<ConnectionCard> & Pick<ConnectionCard, "id" | "name">): ConnectionCard {
  return {
    asset_classes: [], needs_key: true, key_fields: ["key"], key_state: "missing", enabled: true, state: "not_connected", data_state: "live",
    error_code: null, error_message: "", symbols_in_use: 0, max_symbols: 50, priority: {}, premium: false, available: true, ...over,
  };
}

function sixCards(): ConnectionCard[] {
  return [
    card({ id: "coinbase", name: "Coinbase", asset_classes: ["crypto"], needs_key: false, key_fields: [], key_state: "not_required", state: "connected", max_symbols: 100, priority: { crypto: 0 } }),
    card({ id: "alpaca", name: "Alpaca", asset_classes: ["equity"], key_fields: ["key_id", "secret"], max_symbols: 30, priority: { equity: 0 } }),
    card({ id: "finnhub", name: "Finnhub", asset_classes: ["equity", "forex", "crypto"], data_state: "delayed", max_symbols: 50, priority: { equity: 1, forex: 0, crypto: 1 } }),
    card({ id: "siomtrading_futures", name: "SiomTrading Futures", asset_classes: ["future"], max_symbols: 20, available: false, priority: { future: 0 } }),
    card({ id: "databento", name: "Databento", asset_classes: ["future", "equity"], premium: true, max_symbols: 100, priority: { future: 1, equity: 2 } }),
    card({ id: "massive", name: "Massive", asset_classes: ["future"], premium: true, max_symbols: 50, priority: { future: 2 } }),
  ];
}

const GROUPS: ConnectionsList["groups"] = {
  equity: ["alpaca", "finnhub", "databento"],
  crypto: ["coinbase", "finnhub"],
  forex: ["finnhub"],
  future: ["siomtrading_futures", "databento", "massive"],
};

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** The routes of /api/connections, with a store that remembers the keys apart from what the list says. */
class FakeConnections {
  cards = sixCards();
  groups: ConnectionsList["groups"] = structuredClone(GROUPS);
  storeAvailable = true;
  calls: Array<{ method: string; path: string; body: unknown }> = [];
  store = new Map<string, unknown>();
  testAnswer: Record<string, unknown> = { ok: true, state: "connected", data_state: "live", latency_ms: 123.4, symbol: "BTC-USD", first_data: true, error_code: null, message: "" };
  priorityFails = false;

  find(id: string) {
    return this.cards.find((c) => c.id === id)!;
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname, body });
    const parts = url.pathname.split("/").filter(Boolean); // api, connections, id, action
    if (parts[1] !== "connections") return reply({ detail: "unexpected" }, 404);
    if (parts.length === 2 && method === "GET") return reply({ connections: this.cards, groups: this.groups, store_available: this.storeAvailable });
    if (parts[2] === "priority" && method === "PUT") {
      if (this.priorityFails) return reply({ detail: { code: "connections.bad_priority", message: "x" } }, 422);
      this.groups = { ...this.groups, [body.asset_class]: body.order };
      return reply({ groups: this.groups });
    }
    const target = this.find(parts[2]!);
    if (parts[3] === "key" && method === "PUT") {
      if (!this.storeAvailable) return reply({ detail: { code: "connections.store_unavailable", message: "System credential store not available" } }, 503);
      this.store.set(target.id, body);
      target.key_state = "saved";
      return reply({ id: target.id, key_state: "saved" });
    }
    if (parts[3] === "key" && method === "DELETE") {
      this.store.delete(target.id);
      target.key_state = "missing";
      return reply({ id: target.id, key_state: "missing" });
    }
    if (parts[3] === "enabled" && method === "PUT") {
      target.enabled = body.enabled;
      return reply(target);
    }
    if (parts[3] === "test" && method === "POST") return reply(this.testAnswer);
    return reply({ detail: "unexpected" }, 404);
  };

  sent(method: string, tail: string) {
    return this.calls.filter((c) => c.method === method && c.path.endsWith(tail));
  }
}

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

const cardOf = (id: string) => screen.getByTestId("connection-" + id);

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

describe("C1: the screen", () => {
  it("shows the six cards at once, grouped by kind of data, in this order", async () => {
    await show();
    const groups = [...document.querySelectorAll("[data-group]")].map((g) => g.getAttribute("data-group"));
    expect(groups).toEqual(["crypto", "equity", "mixed", "future", "priority"]);
    const inGroup = (group: string) => [...document.querySelector(`[data-group="${group}"]`)!.querySelectorAll("[data-provider]")].filter((e) => e.tagName === "ARTICLE").map((e) => e.getAttribute("data-provider"));
    expect(inGroup("crypto")).toEqual(["coinbase"]);
    expect(inGroup("equity")).toEqual(["alpaca"]);
    expect(inGroup("mixed")).toEqual(["finnhub"]);
    expect(inGroup("future")).toEqual(["siomtrading_futures", "databento", "massive"]);
    expect(document.querySelectorAll("article[data-provider]")).toHaveLength(6);
    for (const name of ["Coinbase", "Alpaca", "Finnhub", "SiomTrading Futures", "Databento", "Massive"]) expect(screen.getByRole("article", { name })).toBeVisible();
    expect(screen.getByRole("heading", { name: t("connections.group.equity") })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: t("connections.group.mixed") })).toBeInTheDocument();
  });

  it("the futures cards follow the futures priority", async () => {
    backend.find("siomtrading_futures").priority = { future: 2 };
    backend.find("massive").priority = { future: 0 };
    await show();
    const order = [...document.querySelector('[data-group="future"]')!.querySelectorAll("article")].map((e) => e.getAttribute("data-provider"));
    expect(order).toEqual(["massive", "databento", "siomtrading_futures"]);
  });

  it("Coinbase has a switch and no key field; Alpaca has two fields and the others one", async () => {
    await show();
    const coinbase = within(cardOf("coinbase"));
    expect(coinbase.getByRole("switch", { name: t("connections.toggle", { name: "Coinbase" }) })).toBeChecked();
    expect(cardOf("coinbase").querySelector("input")).toBeNull();
    expect(coinbase.queryByTestId("conn-key-state")).toBeNull();
    expect(coinbase.queryByRole("button", { name: t("connections.key.remove") })).toBeNull();
    expect(coinbase.getByRole("button", { name: t("connections.test") })).toBeEnabled();
    expect(cardOf("alpaca").querySelectorAll("input")).toHaveLength(2);
    expect(within(cardOf("alpaca")).getByLabelText(t("connections.field.keyId"))).toHaveAttribute("type", "password");
    expect(within(cardOf("alpaca")).getByLabelText(t("connections.field.secret"))).toHaveAttribute("type", "password");
    for (const id of ["finnhub", "databento", "massive"]) expect(cardOf(id).querySelectorAll("input")).toHaveLength(1);
    expect(within(cardOf("siomtrading_futures")).getByLabelText(t("connections.field.token"))).toBeInTheDocument();
    expect(within(cardOf("finnhub")).getByLabelText(t("connections.field.key"))).toBeInTheDocument();
  });

  it("each card says its state, its kind of data, the symbols in use over the maximum, and Saved or Missing", async () => {
    backend.find("alpaca").symbols_in_use = 3;
    backend.find("finnhub").key_state = "saved";
    await show();
    expect(within(cardOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.connected"));
    expect(within(cardOf("coinbase")).getByTestId("conn-data")).toHaveTextContent(t("connections.data.live"));
    expect(within(cardOf("coinbase")).getByTestId("conn-symbols")).toHaveTextContent(t("connections.symbols", { used: 0, max: 100 }));
    expect(within(cardOf("alpaca")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.notConnected"));
    expect(within(cardOf("alpaca")).getByTestId("conn-symbols")).toHaveTextContent(t("connections.symbols", { used: 3, max: 30 }));
    expect(within(cardOf("alpaca")).getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.missing"));
    expect(within(cardOf("finnhub")).getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.saved"));
    expect(within(cardOf("finnhub")).getByTestId("conn-data")).toHaveTextContent(t("connections.data.delayed"));
  });

  it("shows the notes of the cards: IEX, the limits, premium, and the source that is not available yet", async () => {
    await show();
    expect(within(cardOf("alpaca")).getByText(t("connections.note.alpaca"))).toBeInTheDocument();
    expect(within(cardOf("alpaca")).getByText(t("connections.note.limit", { max: 30 }))).toBeInTheDocument();
    expect(within(cardOf("finnhub")).getByText(t("connections.note.limit", { max: 50 }))).toBeInTheDocument();
    expect(within(cardOf("databento")).getByText(t("connections.note.premium"))).toBeInTheDocument();
    expect(within(cardOf("massive")).getByText(t("connections.note.premium"))).toBeInTheDocument();
    expect(within(cardOf("coinbase")).queryByText(t("connections.note.premium"))).toBeNull();
    expect(within(cardOf("siomtrading_futures")).getByTestId("conn-unavailable")).toBeInTheDocument();
    expect(within(cardOf("siomtrading_futures")).getByRole("button", { name: t("connections.test") })).toBeDisabled();
  });

  it("an error card shows the catalog text of its code, in the active language", async () => {
    Object.assign(backend.find("alpaca"), { state: "error", error_code: "connections.auth_failed", error_message: "server text" });
    await show();
    const alpaca = within(cardOf("alpaca"));
    expect(alpaca.getByTestId("conn-state")).toHaveTextContent(t("connections.state.error"));
    const english = t("connections.err.auth_failed");
    expect(alpaca.getByTestId("conn-error")).toHaveTextContent(english);
    act(() => setLocale("es"));
    expect(t("connections.err.auth_failed")).not.toBe(english);
    expect(within(cardOf("alpaca")).getByTestId("conn-error")).toHaveTextContent(t("connections.err.auth_failed"));
  });

  it("an unknown code falls back to the text the server sent", async () => {
    Object.assign(backend.find("alpaca"), { state: "error", error_code: "connections.brand_new", error_message: "Server wording" });
    await show();
    expect(within(cardOf("alpaca")).getByTestId("conn-error")).toHaveTextContent("Server wording");
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

describe("C3: keys never come back", () => {
  it("saves the key, clears the field and the DOM only says Saved", async () => {
    await show();
    const finnhub = within(cardOf("finnhub"));
    await userEvent.type(finnhub.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(finnhub.getByRole("button", { name: t("connections.key.save") }));
    await waitFor(() => expect(finnhub.getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.saved")));
    expect(backend.sent("PUT", "/finnhub/key")[0]!.body).toEqual({ key: SECRET });
    expect(backend.store.get("finnhub")).toEqual({ key: SECRET });
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.textContent).not.toContain(SECRET);
    for (const input of document.querySelectorAll("input")) expect((input as HTMLInputElement).value).toBe("");
    expect(finnhub.getByRole("button", { name: t("connections.key.remove") })).toBeEnabled();
    // the list the panel reloaded carries no key either
    expect(JSON.stringify(backend.cards)).not.toContain(SECRET);
  });

  it("Alpaca sends Key ID and Secret together and keeps neither", async () => {
    await show();
    const alpaca = within(cardOf("alpaca"));
    const save = alpaca.getByRole("button", { name: t("connections.key.save") });
    expect(save).toBeDisabled();
    await userEvent.type(alpaca.getByLabelText(t("connections.field.keyId")), "AKID-1234567890");
    expect(save).toBeDisabled(); // both are needed
    await userEvent.type(alpaca.getByLabelText(t("connections.field.secret")), SECRET);
    await userEvent.click(save);
    await waitFor(() => expect(alpaca.getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.saved")));
    expect(backend.sent("PUT", "/alpaca/key")[0]!.body).toEqual({ key_id: "AKID-1234567890", secret: SECRET });
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain("AKID-1234567890");
    for (const input of document.querySelectorAll("input")) expect((input as HTMLInputElement).value).toBe("");
  });

  it("a failed save does not repeat the key anywhere either", async () => {
    backend.handle = async () => reply({ detail: { code: "connections.bad_key", message: "That key is empty" } }, 422);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => (String(input).endsWith("/key") ? backend.handle(input, init) : new FakeConnections().handle(input, init))));
    await show();
    const finnhub = within(cardOf("finnhub"));
    await userEvent.type(finnhub.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(finnhub.getByRole("button", { name: t("connections.key.save") }));
    expect(await finnhub.findByTestId("conn-action-error")).toHaveTextContent(t("connections.err.bad_key"));
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("Remove key asks the backend to delete it and the card goes back to Missing", async () => {
    backend.find("finnhub").key_state = "saved";
    await show();
    const finnhub = within(cardOf("finnhub"));
    await userEvent.click(finnhub.getByRole("button", { name: t("connections.key.remove") }));
    await waitFor(() => expect(finnhub.getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.missing")));
    expect(backend.sent("DELETE", "/finnhub/key")).toHaveLength(1);
    expect(finnhub.getByRole("button", { name: t("connections.key.remove") })).toBeDisabled();
  });

  it("without a credential store it says so and cannot save", async () => {
    backend.storeAvailable = false;
    await show();
    expect(screen.getByTestId("store-unavailable")).toHaveTextContent("System credential store not available");
    const finnhub = within(cardOf("finnhub"));
    expect(finnhub.getByLabelText(t("connections.field.key"))).toBeDisabled();
    expect(finnhub.getByRole("button", { name: t("connections.key.save") })).toBeDisabled();
  });

  it("learns the store is gone when the save answers 503, and the key is not kept", async () => {
    await show();
    backend.storeAvailable = false;
    const finnhub = within(cardOf("finnhub"));
    await userEvent.type(finnhub.getByLabelText(t("connections.field.key")), SECRET);
    await userEvent.click(finnhub.getByRole("button", { name: t("connections.key.save") }));
    expect(await screen.findByTestId("store-unavailable")).toBeInTheDocument();
    expect(finnhub.getByTestId("conn-action-error")).toHaveTextContent(t("connections.err.store_unavailable"));
    expect(backend.store.size).toBe(0);
  });
});

describe("switch and test", () => {
  it("the switch turns a provider off and on", async () => {
    await show();
    const toggle = within(cardOf("coinbase")).getByRole("switch");
    await userEvent.click(toggle);
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(backend.sent("PUT", "/coinbase/enabled")[0]!.body).toEqual({ enabled: false });
    await userEvent.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
  });

  it("Test connection shows Connected, the kind of data and the latency", async () => {
    await show();
    await userEvent.click(within(cardOf("coinbase")).getByRole("button", { name: t("connections.test") }));
    const result = await within(cardOf("coinbase")).findByTestId("conn-test-result");
    expect(result).toHaveAttribute("data-ok", "true");
    expect(result).toHaveTextContent(t("connections.test.ok", { data: t("connections.data.live"), ms: 123 }));
  });

  it("Test connection shows the catalog error when it fails", async () => {
    backend.testAnswer = { ok: false, state: "error", data_state: "live", latency_ms: null, symbol: "SPY", first_data: false, error_code: "connections.auth_failed", message: "x" };
    await show();
    await userEvent.click(within(cardOf("alpaca")).getByRole("button", { name: t("connections.test") }));
    const result = await within(cardOf("alpaca")).findByTestId("conn-test-result");
    expect(result).toHaveAttribute("data-ok", "false");
    expect(result).toHaveTextContent(t("connections.test.fail", { reason: t("connections.err.auth_failed") }));
  });
});

describe("C7: the state follows the engine without a reload", () => {
  const status = (provider: string, state: string, extra: Record<string, unknown> = {}) => ({
    type: "connection_status", provider, source: provider, epoch: "e1", state, data_state: "live", error_code: null, enabled: true, key_state: "not_required", symbols_in_use: 0, ...extra,
  });

  it("goes Connected, Reconnecting, Connected as connection_status arrives, with no new request", async () => {
    await show();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    const pill = within(cardOf("coinbase")).getByTestId("conn-state");
    expect(pill).toHaveTextContent(t("connections.state.connected"));
    const requestsBefore = backend.calls.length;
    act(() => socket.serverSend(status("coinbase", "reconnecting")));
    expect(pill).toHaveTextContent(t("connections.state.reconnecting"));
    act(() => socket.serverSend(status("coinbase", "connected", { symbols_in_use: 2 })));
    expect(pill).toHaveTextContent(t("connections.state.connected"));
    expect(within(cardOf("coinbase")).getByTestId("conn-symbols")).toHaveTextContent(t("connections.symbols", { used: 2, max: 100 }));
    expect(backend.calls.length).toBe(requestsBefore);
  });

  it("an error from the engine shows its catalog text, and a changed key or switch is applied", async () => {
    await show();
    const socket = await waitFor(() => FakeSocket.last());
    act(() => socket.serverOpen());
    act(() => socket.serverSend(status("alpaca", "error", { error_code: "connections.connection_limit", key_state: "saved" })));
    expect(within(cardOf("alpaca")).getByTestId("conn-error")).toHaveTextContent(t("connections.err.connection_limit"));
    expect(within(cardOf("alpaca")).getByTestId("conn-key-state")).toHaveTextContent(t("connections.key.saved"));
    act(() => socket.serverSend(status("coinbase", "not_connected", { enabled: false })));
    expect(within(cardOf("coinbase")).getByRole("switch")).not.toBeChecked();
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
    const lists = () => backend.calls.filter((c) => c.method === "GET").length;
    const before = lists();
    backend.find("coinbase").state = "error";
    backend.find("coinbase").error_code = "connections.unreachable";
    act(() => socket.serverClose(1011));
    await waitFor(() => expect(FakeSocket.of("/ws/market").length).toBeGreaterThan(1), { timeout: 3000 });
    act(() => FakeSocket.last().serverOpen());
    await waitFor(() => expect(lists()).toBe(before + 1));
    await waitFor(() => expect(within(cardOf("coinbase")).getByTestId("conn-state")).toHaveTextContent(t("connections.state.error")));
  });
});

describe("priority", () => {
  const rows = (cls: string) => [...screen.getByTestId("priority-" + cls).querySelectorAll("li")].map((li) => li.getAttribute("data-provider"));

  it("lists the classes that have more than one provider, in order", async () => {
    await show();
    expect(rows("equity")).toEqual(["alpaca", "finnhub", "databento"]);
    expect(rows("crypto")).toEqual(["coinbase", "finnhub"]);
    expect(rows("future")).toEqual(["siomtrading_futures", "databento", "massive"]);
    expect(screen.queryByTestId("priority-forex")).toBeNull(); // one provider: nothing to order
  });

  it("the arrow buttons move a provider and send the new order", async () => {
    await show();
    await userEvent.click(within(screen.getByTestId("priority-equity")).getByRole("button", { name: t("connections.priority.down", { name: "Alpaca" }) }));
    await waitFor(() => expect(backend.sent("PUT", "/connections/priority")).toHaveLength(1));
    expect(backend.sent("PUT", "/connections/priority")[0]!.body).toEqual({ asset_class: "equity", order: ["finnhub", "alpaca", "databento"] });
    expect(rows("equity")).toEqual(["finnhub", "alpaca", "databento"]);
    expect(within(screen.getByTestId("priority-equity")).getAllByRole("button", { name: /^Move/ })[0]).toBeDisabled(); // first cannot go up
  });

  it("dragging a row onto another reorders and sends it", async () => {
    await show();
    const list = screen.getByTestId("priority-future");
    const items = list.querySelectorAll("li");
    const data = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(items[2]!, { dataTransfer: data });
    fireEvent.dragOver(items[0]!, { dataTransfer: data });
    fireEvent.drop(items[0]!, { dataTransfer: data });
    await waitFor(() => expect(backend.sent("PUT", "/connections/priority")).toHaveLength(1));
    expect(backend.sent("PUT", "/connections/priority")[0]!.body).toEqual({ asset_class: "future", order: ["massive", "siomtrading_futures", "databento"] });
  });

  it("puts the order back and says why when the engine refuses it", async () => {
    backend.priorityFails = true;
    await show();
    await userEvent.click(within(screen.getByTestId("priority-crypto")).getByRole("button", { name: t("connections.priority.down", { name: "Coinbase" }) }));
    expect(await screen.findByText(t("connections.err.bad_priority"))).toBeInTheDocument();
    expect(rows("crypto")).toEqual(["coinbase", "finnhub"]);
  });
});

describe("C6: the group of a class comes forward", () => {
  it("reads #/connections and #/connections?class=equity", () => {
    expect(parseConnectionsHash("#/connections")).toEqual({ assetClass: null });
    expect(parseConnectionsHash("#/connections?class=equity")).toEqual({ assetClass: "equity" });
    expect(parseConnectionsHash("#/chart")).toBeNull();
    expect(parseConnectionsHash("")).toBeNull();
  });

  it("marks the providers and the priority row of the class", async () => {
    requestFocus("equity");
    await show();
    const marked = [...document.querySelectorAll("[data-focus='true']")].map((e) => e.getAttribute("data-testid"));
    expect(marked.sort()).toEqual(["connection-alpaca", "connection-databento", "connection-finnhub", "priority-equity"]);
    expect(cardOf("coinbase")).not.toHaveAttribute("data-focus");
  });

  it("scrolls the group into view", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      requestFocus("future");
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
    // the same link works again
    act(() => {
      window.history.replaceState(null, "", "/#/connections?class=future");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(getFocus().assetClass).toBe("future");
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
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => (fail ? Promise.resolve(reply({ detail: { code: "network", message: "Down" } }, 502)) : backend.handle(input, init))));
    renderWithApp(<ConnectionsScreen />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(t("connections.error.title"));
    fail = false;
    await userEvent.click(within(alert).getByRole("button", { name: t("ui.error.retry") }));
    expect(await screen.findByTestId("connection-coinbase")).toBeInTheDocument();
  });
});
