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
  await entryCall("/api/system/shutdown", t("shutdown.failed"));
  enterOff();
}

/** "Use Hokma here in the browser": the same entry call; the engine opens a fresh session in the default browser. */
export async function openInBrowserFromEntry(): Promise<void> {
  await entryCall("/api/system/browser", t("browser.failed"));
}

async function entryCall(path: string, failed: string): Promise<void> {
  const headers = { "Accept-Language": getLocale() };
  let answer: Response;
  try {
    // the engine hands this page a single-use nonce first (it lives 60 seconds); the call carries it back
    const asked = await fetch("/api/system/shutdown-nonce", { headers, cache: "no-store" });
    const nonce = asked.ok ? ((await asked.json()) as { nonce?: unknown }).nonce : undefined;
    if (typeof nonce !== "string") throw new ApiError(asked.status, "shutdown.nonce", failed);
    answer = await fetch(path, { method: "POST", headers: { ...headers, "X-Hokma-Shutdown-Nonce": nonce } });
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "network", t("api.network"));
  }
  if (answer.status === 202) return;
  let code = "http_error";
  let message = failed;
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

/** The app side: open this same Hokma in the browser with a fresh session (the window stops being the owner). */
export async function openInBrowser(): Promise<void> {
  await request("/api/system/browser", { method: "POST" });
}

export async function getBrowserPref(): Promise<boolean> {
  const answer = await request("/api/system/browser-pref");
  return ((await answer.json()) as { always?: unknown }).always === true;
}

export async function setBrowserPref(always: boolean): Promise<void> {
  await request("/api/system/browser-pref", { method: "PUT", body: { always } });
}
