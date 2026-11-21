import { useSyncExternalStore } from "react";
import type { AssetClass } from "./types";
import { ASSET_CLASSES } from "./types";

/**
 * What the Connections screen should bring forward. The chart link and the address `#/connections?class=` ask for
 * an asset class; the session notice asks for a provider (`#/connections?focus=schwab&login=1`), and `login` opens the
 * second step of the Schwab sign-in. `nonce` changes on every request, so asking for the same thing twice works.
 */
export interface ConnectionsFocus {
  assetClass: AssetClass | null;
  provider: string | null;
  login: boolean;
  nonce: number;
}

let current: ConnectionsFocus = { assetClass: null, provider: null, login: false, nonce: 0 };
/** The request whose sign-in dialog was already opened: leaving the screen and coming back does not open it again. */
let loginHandled = 0;
const listeners = new Set<() => void>();

const PROVIDER_ID = /^[a-z][a-z0-9_]{0,40}$/;

export function getFocus(): ConnectionsFocus {
  return current;
}

export function requestFocus(assetClass: string | null | undefined, options: { provider?: string | null; login?: boolean } = {}): void {
  const known = (ASSET_CLASSES as readonly string[]).includes(assetClass ?? "") ? (assetClass as AssetClass) : null;
  const provider = options.provider && PROVIDER_ID.test(options.provider) ? options.provider : null;
  current = { assetClass: known, provider, login: options.login === true && provider !== null, nonce: current.nonce + 1 };
  listeners.forEach((listener) => listener());
}

/** True once per request that asked for the sign-in step (`login=1`). */
export function takeLogin(): boolean {
  if (!current.login || loginHandled === current.nonce) return false;
  loginHandled = current.nonce;
  return true;
}

export function useFocus(): ConnectionsFocus {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getFocus,
    getFocus,
  );
}

export interface ConnectionsRoute {
  assetClass: string | null;
  provider: string | null;
  login: boolean;
}

/** Reads `#/connections`, `#/connections?class=equity` and `#/connections?focus=schwab&login=1`. Returns null for any other address. */
export function parseConnectionsHash(hash: string): ConnectionsRoute | null {
  const match = /^#\/connections(?:\?(.*))?$/.exec(hash);
  if (!match) return null;
  const query = new URLSearchParams(match[1] ?? "");
  return { assetClass: query.get("class"), provider: query.get("focus"), login: query.get("login") === "1" };
}

/** Link target of "Connect a live source". */
export function connectionsHref(assetClass: string | null | undefined): string {
  return assetClass ? "#/connections?class=" + encodeURIComponent(assetClass) : "#/connections";
}

/** Address of the session notice button: Connections with Schwab forward and its sign-in step open. */
export const SCHWAB_LOGIN_HASH = "#/connections?focus=schwab&login=1";
