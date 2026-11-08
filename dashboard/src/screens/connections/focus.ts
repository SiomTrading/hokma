import { useSyncExternalStore } from "react";
import type { AssetClass } from "./types";
import { ASSET_CLASSES } from "./types";

/**
 * Which asset class the Connections screen should bring forward. The chart link and the address `#/connections?class=`
 * set it; the screen shows the group of that class and scrolls to it. `nonce` changes on every request, so asking
 * for the same class twice works.
 */
export interface ConnectionsFocus {
  assetClass: AssetClass | null;
  nonce: number;
}

let current: ConnectionsFocus = { assetClass: null, nonce: 0 };
const listeners = new Set<() => void>();

export function getFocus(): ConnectionsFocus {
  return current;
}

export function requestFocus(assetClass: string | null | undefined): void {
  const known = (ASSET_CLASSES as readonly string[]).includes(assetClass ?? "") ? (assetClass as AssetClass) : null;
  current = { assetClass: known, nonce: current.nonce + 1 };
  listeners.forEach((listener) => listener());
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

/** Reads `#/connections` and `#/connections?class=equity`. Returns null for any other address. */
export function parseConnectionsHash(hash: string): { assetClass: string | null } | null {
  const match = /^#\/connections(?:\?(.*))?$/.exec(hash);
  if (!match) return null;
  return { assetClass: new URLSearchParams(match[1] ?? "").get("class") };
}

/** Link target of "Connect a live source". */
export function connectionsHref(assetClass: string | null | undefined): string {
  return assetClass ? "#/connections?class=" + encodeURIComponent(assetClass) : "#/connections";
}
