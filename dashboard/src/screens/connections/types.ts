export type AssetClass = "equity" | "crypto" | "forex" | "future";
export const ASSET_CLASSES: readonly AssetClass[] = ["equity", "crypto", "forex", "future"];

export type KeyState = "saved" | "missing" | "not_required";
export type ConnectionState = "not_connected" | "connected" | "reconnecting" | "error";
export type DataState = "live" | "delayed";

/** One provider card of `GET /api/connections`. */
export interface ConnectionCard {
  id: string;
  name: string;
  asset_classes: AssetClass[];
  needs_key: boolean;
  /** Names of the secret fields: `["key"]`, or `["key_id", "secret"]` for Alpaca. */
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
  premium: boolean;
  available: boolean;
}

export interface ConnectionsList {
  connections: ConnectionCard[];
  groups: Partial<Record<AssetClass, string[]>>;
  store_available: boolean;
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
