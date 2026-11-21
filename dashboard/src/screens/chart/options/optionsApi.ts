import { api, ApiError } from "@/api";
import { t } from "@/i18n";
import type { ChainResponse, ExpirationsResponse, UnderlyingBar } from "./types";

const enc = encodeURIComponent;

/** The three routes of the option chain. Errors come as `options.*` codes (409 no connection, 401 key refused, 422, 429, 502). */
export const optionsApi = {
  underlying: (symbol: string, signal?: AbortSignal) =>
    api.get<UnderlyingBar>(`/api/market/options/underlying?symbol=${enc(symbol)}`, { signal }),
  expirations: (symbol: string, signal?: AbortSignal) =>
    api.get<ExpirationsResponse>(`/api/market/options/expirations?symbol=${enc(symbol)}`, { signal }),
  chain: (symbol: string, dates: readonly string[], strikes: string, refresh: boolean, signal?: AbortSignal) =>
    api.get<ChainResponse>(
      `/api/market/options/chain?symbol=${enc(symbol)}&expirations=${enc(dates.join(","))}&strikes=${enc(strikes)}${refresh ? "&refresh=true" : ""}`,
      { signal },
    ),
};

/** The short code of an error (`no_connection`, `auth_failed`...), or "upstream" for anything that is not an options code. */
export function optionsCode(cause: unknown): string {
  if (cause instanceof ApiError && cause.code.startsWith("options.")) return cause.code.slice("options.".length);
  return "upstream";
}

/** The sentence of an error in the active language. */
export function optionsErrorText(code: string): string {
  const key = `options.err.${code}`;
  const text = t(key);
  return text === key ? t("options.err.upstream") : text;
}
