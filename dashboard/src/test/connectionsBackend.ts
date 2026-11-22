import type { AddAnswer, ConnectionCard, SchwabAccountPanel, TestResult, UseOrders } from "@/screens/connections/types";

/** Invented values: nothing here is a real key, token, account number or hash. */
export const SECRET = "sk-live-VERY-SECRET-value-98765";
export const SCHWAB_AUTH_URL = "https://auth.example.test/authorize?client_id=FAKEID&state=fake-state";
export const SCHWAB_REDIRECT = "https://127.0.0.1:8182/?code=FAKECODE&state=fake-state";

export const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
export const apiError = (status: number, code: string, message = "server text") => reply({ detail: { code, message } }, status);

export function card(over: Partial<ConnectionCard> & Pick<ConnectionCard, "id" | "name">): ConnectionCard {
  return {
    asset_classes: [], needs_key: true, key_fields: ["key"], key_state: "missing", enabled: true, state: "not_connected", data_state: "live",
    error_code: null, error_message: "", symbols_in_use: 0, max_symbols: 50, priority: {}, available: true,
    added: false, auth: "key", uses: [], session: null, ...over,
  };
}

/** The catalog as 1.0.9 sends it at the start: only Coinbase (no key, switched on) is connected. */
export function startCards(): ConnectionCard[] {
  return [
    card({ id: "schwab", name: "Schwab", asset_classes: ["equity"], key_fields: ["client_id", "client_secret"], auth: "oauth", max_symbols: 100, uses: ["equity", "bars", "options"], priority: { equity: 0 } }),
    card({ id: "alpaca", name: "Alpaca Trading", asset_classes: ["equity"], key_fields: ["key_id", "secret"], max_symbols: 30, uses: ["equity", "bars", "options", "news"], priority: { equity: 1 } }),
    card({ id: "finnhub", name: "Finnhub", asset_classes: ["equity", "forex", "crypto"], data_state: "delayed", uses: ["equity", "bars", "crypto", "forex", "news"], priority: { equity: 2, forex: 0, crypto: 1 } }),
    card({ id: "siomtrading_futures", name: "SiomTrading Futures", asset_classes: ["future"], max_symbols: 20, uses: ["future"], priority: { future: 0 } }),
    card({ id: "coinbase", name: "Coinbase", asset_classes: ["crypto"], needs_key: false, key_fields: [], key_state: "not_required", added: true, state: "connected", max_symbols: 100, uses: ["crypto"], priority: { crypto: 0 } }),
  ];
}

export const DEFAULT_USES: UseOrders = {
  equity: ["schwab", "alpaca", "finnhub"],
  bars: ["schwab", "alpaca", "finnhub"],
  options: ["schwab", "alpaca"],
  future: ["siomtrading_futures"],
  crypto: ["coinbase", "finnhub"],
  forex: ["finnhub"],
  news: ["finnhub", "alpaca"],
};

export const OK_TEST: TestResult = { ok: true, state: "connected", data_state: "live", latency_ms: 240.4, symbol: "SPY", first_data: true, error_code: null, message: "" };
export const BAD_KEY_TEST: TestResult = { ok: false, state: "error", data_state: "live", latency_ms: null, symbol: "SPY", first_data: false, error_code: "connections.auth_failed", message: "server text" };

export const ACCOUNT_PANEL: SchwabAccountPanel = {
  accounts: [
    {
      account_ref: "A1",
      mask: "••••1234",
      type: "MARGIN",
      currentBalances: { equity: 125000.5, buyingPower: 250000, availableFunds: 98000.25 },
      initialBalances: { liquidationValue: 124000 },
      positions: [
        { instrument: { symbol: "AAPL", assetType: "EQUITY" }, longQuantity: 10, shortQuantity: 0, averagePrice: 180.5, marketValue: 1900, currentDayProfitLoss: 12.5, currentDayProfitLossPercentage: 0.66 },
      ],
      orders: [
        { orderLegCollection: [{ instruction: "BUY", quantity: 5, instrument: { symbol: "MSFT" } }], orderType: "LIMIT", price: 400.25, duration: "DAY", status: "WORKING", filledQuantity: 0, remainingQuantity: 5, enteredTime: "2026-11-20T15:00:00Z" },
      ],
    },
  ],
};

type Call = { method: string; path: string; body: unknown };

/** The routes of /api/connections, /api/mail and the Schwab sign-in, with a store that remembers the keys apart from what the list says. */
export class FakeConnections {
  cards = startCards();
  uses: UseOrders = structuredClone(DEFAULT_USES);
  storeAvailable = true;
  calls: Call[] = [];
  store = new Map<string, unknown>();
  mail = { connected: false, email: null as string | null, store_available: true, apppasswords_url: "https://myaccount.google.com/apppasswords" };
  testAnswer: TestResult = OK_TEST;
  /** Providers whose "add" fails the test: the answer is added=false and nothing is kept. */
  failAdd = new Map<string, TestResult>();
  priorityFails = false;
  finishFails: { status: number; code: string } | null = null;
  startFails: { status: number; code: string } | null = null;
  account: SchwabAccountPanel | { status: number; code: string } = ACCOUNT_PANEL;

  find(id: string) {
    return this.cards.find((c) => c.id === id)!;
  }

  handle = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    this.calls.push({ method, path: url.pathname, body });
    const parts = url.pathname.split("/").filter(Boolean); // api, connections, id, action...
    if (parts[1] === "mail") return reply(this.mail);
    if (parts[1] !== "connections") return reply({ detail: "unexpected" }, 404);
    if (parts.length === 2 && method === "GET") return reply({ connections: this.cards, groups: {}, uses: this.uses, store_available: this.storeAvailable });
    if (parts[2] === "priority" && method === "PUT") {
      if (this.priorityFails) return apiError(422, "connections.bad_priority");
      this.uses = { ...this.uses, [body.asset_class]: body.order };
      return reply({ uses: this.uses });
    }
    if (parts[2] === "schwab" && parts[3] === "login") return this.schwabLogin(parts[4]!);
    if (parts[2] === "schwab" && parts[3] === "account") {
      return "accounts" in this.account ? reply(this.account) : apiError(this.account.status, this.account.code);
    }
    const target = this.find(parts[2]!);
    if (parts[3] === "key" && method === "PUT") {
      if (!this.storeAvailable) return apiError(503, "connections.store_unavailable", "System credential store not available");
      this.store.set(target.id, body);
      target.key_state = "saved";
      return reply({ id: target.id, key_state: "saved" });
    }
    if (parts[3] === "enabled" && method === "PUT") {
      target.enabled = body.enabled;
      return reply(target);
    }
    if (parts[3] === "test" && method === "POST") return reply(this.testAnswer);
    if (parts[3] === "add" && method === "POST") return this.add(target, body);
    if (parts[3] === "remove" && method === "POST") {
      this.store.delete(target.id);
      Object.assign(target, { added: false, enabled: target.needs_key ? target.enabled : false, key_state: target.needs_key ? "missing" : "not_required", state: "not_connected", session: null });
      return reply(target);
    }
    return reply({ detail: "unexpected" }, 404);
  };

  private add(target: ConnectionCard, body: unknown): Response {
    if (!this.storeAvailable) return apiError(503, "connections.store_unavailable", "System credential store not available");
    const failed = this.failAdd.get(target.id);
    if (failed) return reply({ added: false, card: target, test: failed } satisfies AddAnswer);
    if (target.needs_key) this.store.set(target.id, body);
    Object.assign(target, { added: true, enabled: true, key_state: target.needs_key ? "saved" : "not_required", state: "connected" });
    return reply({ added: true, card: target, test: this.testAnswer } satisfies AddAnswer);
  }

  private schwabLogin(action: string): Response {
    const schwab = this.find("schwab");
    if (action === "start") return this.startFails ? apiError(this.startFails.status, this.startFails.code) : reply({ auth_url: SCHWAB_AUTH_URL });
    if (action === "open") return reply({ opened: true });
    if (action === "finish") {
      if (this.finishFails) return apiError(this.finishFails.status, this.finishFails.code);
      Object.assign(schwab, {
        added: true, enabled: true, key_state: "saved", state: "connected",
        session: { state: "signed_in", session_expires_at: Math.floor(Date.now() / 1000) + 7 * 86400 - 5, accounts: [{ account_ref: "A1", mask: "••••1234" }] },
      });
      return reply({ card: schwab, test: this.testAnswer });
    }
    return reply({ detail: "unexpected" }, 404);
  }

  sent(method: string, tail: string) {
    return this.calls.filter((c) => c.method === method && c.path.endsWith(tail));
  }
}
