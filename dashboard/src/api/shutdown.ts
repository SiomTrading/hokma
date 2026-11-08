import { getLocale, t } from "@/i18n";
import { ApiError, request } from "./client";
import { enterOff } from "./restart";

/**
 * Shut down Hokma from the panel (the Shut down Hokma button): `POST /api/system/shutdown` with the session token.
 * With the answer 202 this tab goes to the shutdown screen at once; the other tabs get there when their socket
 * closes with the reason `shutting_down`.
 */
export async function shutdownHokma(): Promise<void> {
  await request("/api/system/shutdown", { method: "POST" });
  enterOff();
}

/**
 * The same call from the entry screen of a browser that has no session: no token, only the page itself as origin.
 * The engine refuses it while someone has Hokma open with a session (409 `shutdown.in_use`).
 */
export async function shutdownFromEntry(): Promise<void> {
  const headers = { "Accept-Language": getLocale() };
  let answer: Response;
  try {
    // the engine hands this page a single-use nonce first (it lives 60 seconds); the call carries it back
    const asked = await fetch("/api/system/shutdown-nonce", { headers, cache: "no-store" });
    const nonce = asked.ok ? ((await asked.json()) as { nonce?: unknown }).nonce : undefined;
    if (typeof nonce !== "string") throw new ApiError(asked.status, "shutdown.nonce", t("shutdown.failed"));
    answer = await fetch("/api/system/shutdown", { method: "POST", headers: { ...headers, "X-Hokma-Shutdown-Nonce": nonce } });
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "network", t("api.network"));
  }
  if (answer.status === 202) {
    enterOff();
    return;
  }
  let code = "http_error";
  let message = t("shutdown.failed");
  try {
    const body = (await answer.json()) as { detail?: { code?: unknown; message?: unknown } };
    if (typeof body.detail?.code === "string") code = body.detail.code;
    if (typeof body.detail?.message === "string") message = body.detail.message;
  } catch {
    /* not JSON: keep the generic message */
  }
  if (code === "shutdown.in_use") message = t("shutdown.inUse");
  throw new ApiError(answer.status, code, message);
}
