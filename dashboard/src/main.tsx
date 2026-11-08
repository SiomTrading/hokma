import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import {
  enterWithCode,
  getToken,
  handoffToOwnHost,
  initToken,
  isStartOrigin,
  refreshSession,
  registerOwner,
  resumeSession,
  startedFromLink,
  takeEntryCode,
} from "@/api";
import { getOwnerOnce } from "@/api/owner";
import "@/shell/autoload";
import { getLocale } from "@/i18n";
import { App } from "./App";

/**
 * Decides how the session starts before anything is painted. Resolves true when the page is leaving for another
 * address (nothing is painted then).
 * - the start link (`#t=` and `#o=` on 127.0.0.1): register the owner, then hand over to the engine's own host, where
 *   the browser key is made. Where that is not possible (Firefox, no IndexedDB or WebCrypto) the session stays here;
 * - a token in memory otherwise (the handoff of a restart Hokma made): register the owner as always; when it is a
 *   handoff (no one-time code) sign in again with the device key so the engine gets the confirmation back;
 * - no token on the engine's own host with `#c=`: enter with that code and make the key;
 * - no token on the engine's own host without it: resume with the key of this browser, so the entry screen does not
 *   flash for a returning owner. 127.0.0.1 keeps no key and never resumes.
 */
async function startSession(): Promise<boolean> {
  initToken();
  const code = takeEntryCode();
  const startOrigin = isStartOrigin();
  if (getToken()) {
    if (startOrigin && startedFromLink()) {
      const registered = await registerOwner();
      return registered ? await handoffToOwnHost() : false;
    }
    void registerOwner();
    if (!getOwnerOnce() && !startOrigin) void refreshSession();
    return false;
  }
  if (startOrigin) return false;
  if (code) await enterWithCode(code);
  else await resumeSession();
  return false;
}

async function main(): Promise<void> {
  if (await startSession()) return;
  document.documentElement.lang = getLocale();
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void main();
