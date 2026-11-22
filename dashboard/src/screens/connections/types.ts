export type AssetClass = "equity" | "crypto" | "forex" | "future";
export const ASSET_CLASSES: readonly AssetClass[] = ["equity", "crypto", "forex", "future"];

/** The kinds of data that have an "order of use" (SPEC-015 4.4). `equity`, `future`, `crypto` and `forex` are asset classes too. */
export type UseKey = "equity" | "bars" | "options" | "future" | "crypto" | "forex" | "news";
/** The rows of "Order of use", in the order the screen shows them. */
export const USES: readonly UseKey[] = ["equity", "bars", "options", "future", "crypto", "forex", "news"];

export type KeyState = "saved" | "missing" | "not_required";
export type ConnectionState = "not_connected" | "connected" | "reconnecting" | "error";
export type DataState = "live" | "delayed";
export type AuthKind = "key" | "oauth";
export type SessionState = "signed_in" | "expiring" | "expired" | "signed_out";

/** Session of a provider that signs in with OAuth (Schwab). Never carries a token, a full account number or a hash. */
export interface SchwabSession {
  state: SessionState;
  /** Epoch seconds or an ISO date: the screen reads both. */
  session_expires_at: number | string | null;
  accounts: Array<{ account_ref: string; mask: string }>;
}

/** One provider card of `GET /api/connections`. */
export interface ConnectionCard {
  id: string;
  name: string;
  asset_classes: AssetClass[];
  needs_key: boolean;
  /** Names of the secret fields: `["key"]`, `["key_id", "secret"]` for Alpaca, `["client_id", "client_secret"]` for Schwab. */
  key_fields: string[];
  key_state: KeyState;
  enabled: boolean;
  state: ConnectionState;
  data_state: DataState;
  error_code: string | null;
  error_message: string;
  symbols_in_use: number;
  max_symbols: number;
  priority: Partial<Record<AssetClass, number>>;
  available: boolean;
  /** Connected in the sense of 4.3 rule 1: key saved (or session), or no key and switched on. */
  added: boolean;
  auth: AuthKind;
  /** The uses this provider can serve. */
  uses: UseKey[];
  /** `broker` (a broker that gives the live data), `free` or null. */
  role?: "broker" | "free" | null;
  session?: SchwabSession | null;
}

export type UseOrders = Partial<Record<UseKey, string[]>>;

export interface ConnectionsList {
  connections: ConnectionCard[];
  groups: Partial<Record<AssetClass, string[]>>;
  /** Order of every use, provider ids with the not connected ones too. Replaces `groups` on the screen. */
  uses: UseOrders;
  store_available: boolean;
  /** The broker the user chose as the source of live data, or null (the first one connected serves). */
  data_source?: string | null;
}

/** Answer of `POST /api/connections/{id}/test`. */
export interface TestResult {
  ok: boolean;
  state: ConnectionState;
  data_state: DataState;
  latency_ms: number | null;
  symbol: string | null;
  first_data: boolean;
  error_code: string | null;
  message: string;
}

/** Answer of `POST /api/connections/{id}/add`: when the test fails `added` is false and the new key was dropped. */
export interface AddAnswer {
  added: boolean;
  card: ConnectionCard;
  test: TestResult;
}

/** Answer of `POST /api/connections/schwab/login/finish`. */
export interface FinishAnswer {
  card: ConnectionCard;
  test: TestResult;
}

/** `connection_status` message of `/ws/market`: the live part of a card. */
export interface ConnectionStatusMessage {
  type: "connection_status";
  provider: string;
  state: ConnectionState;
  data_state: DataState;
  error_code: string | null;
  enabled: boolean;
  key_state: KeyState;
  symbols_in_use: number;
}

/**
 * `GET /api/connections/schwab/account` (5.7): the accounts of the student with Schwab's own field names, each
 * `accountNumber` already replaced by `account_ref` and `mask`. Every number may be missing: a missing one is shown as a dash.
 */
export interface SchwabMoney {
  equity?: number | null;
  buyingPower?: number | null;
  availableFunds?: number | null;
  totalCash?: number | null;
  cashAvailableForTrading?: number | null;
  liquidationValue?: number | null;
}

export interface SchwabPosition {
  instrument?: { symbol?: string; assetType?: string };
  longQuantity?: number | null;
  shortQuantity?: number | null;
  averagePrice?: number | null;
  marketValue?: number | null;
  currentDayProfitLoss?: number | null;
  currentDayProfitLossPercentage?: number | null;
}

export interface SchwabOrder {
  account_ref?: string;
  orderLegCollection?: Array<{ instruction?: string; quantity?: number | null; instrument?: { symbol?: string } }>;
  orderType?: string;
  price?: number | null;
  stopPrice?: number | null;
  duration?: string;
  status?: string;
  filledQuantity?: number | null;
  remainingQuantity?: number | null;
  enteredTime?: string;
}

export interface SchwabAccountEntry {
  account_ref: string;
  mask: string;
  type?: string;
  currentBalances?: SchwabMoney;
  initialBalances?: SchwabMoney;
  positions?: SchwabPosition[];
  orders?: SchwabOrder[];
}

/** The answer of the Schwab data measuring run (SPEC-016 T1): where the file went and the points it measured. No secret in it. */
export interface DiagnosticAnswer {
  ok: boolean;
  file: string;
  messages: number;
  summary: {
    heartbeat_interval_ms: { samples: number; median: number | null; min: number | null; max: number | null };
    chart_equity: { minutes_seen: number; mode: string };
    futures_data: Record<string, number>;
    status_codes: string[];
  };
}

export interface SchwabAccountPanel {
  accounts: SchwabAccountEntry[];
  /** Orders switch of the Schwab broker (Broker tab). False when it does not exist. */
  orders_enabled?: boolean;
  orders?: SchwabOrder[];
  as_of?: string;
}
