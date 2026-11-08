/**
 * How the session token and the owner confirmation of the tab cross a reload that Hokma itself makes (the automatic
 * restart after an update) without ever touching the browser's storage.
 *
 * Both live in variables of the page. Chrome writes `sessionStorage`, `localStorage`, cookies and IndexedDB into
 * the profile on disk, so nothing is kept there. `window.name` is a property of the tab's browsing context that
 * the browser does not persist, but it does survive any navigation of the tab, so it is written under strict terms:
 *
 * - Only `reloadWithHandoff()` writes it, right before Hokma reloads the same URL. Closing the tab, leaving for
 *   another address, a reload with F5, Ctrl+R or the toolbar button write nothing: the session is lost and Hokma has
 *   to be opened again from the terminal. There is no `pagehide`, `beforeunload` or key listener on purpose.
 * - The payload carries the origin and the address (path, query, hash without secrets) it was written for. The
 *   next load takes it out and empties `window.name` at once, and uses it only if both are the same as its own.
 * - If the reload does not happen, the copy is emptied a few seconds later.
 */
const PREFIX = "hokma-handoff:";
const TAKE_BACK_MS = 3000;

type Parts = { t?: string; n?: string };
type Provider = () => string | null;

const providers: Partial<Record<keyof Parts, Provider>> = {};
let cache: Parts | null = null;

/** The address of this page without the secrets a start link may carry in the hash. */
function addressOfThisPage(): string {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  params.delete("t");
  params.delete("o");
  params.delete("c");
  const rest = params.toString();
  return window.location.pathname + window.location.search + (rest ? `#${rest}` : "");
}

function load(): Parts {
  if (cache !== null) return cache;
  cache = {};
  if (typeof window === "undefined") return cache;
  const raw = window.name;
  if (!raw.startsWith(PREFIX)) return cache;
  window.name = ""; // emptied as soon as it is read, whatever it holds
  try {
    const parsed = JSON.parse(raw.slice(PREFIX.length)) as Record<string, unknown> | null;
    if (parsed === null || typeof parsed !== "object") return cache;
    if (parsed.o !== window.location.origin || parsed.u !== addressOfThisPage()) return cache; // not for this page
    if (typeof parsed.t === "string") cache.t = parsed.t;
    if (typeof parsed.n === "string") cache.n = parsed.n;
  } catch {
    /* not ours or damaged: ignored */
  }
  return cache;
}

/** The value a previous load of this tab left, once. */
export function takeHandoff(key: keyof Parts): string | null {
  const parts = load();
  const value = parts[key] ?? null;
  delete parts[key];
  return value;
}

/** Registers who gives the current value of `key` when Hokma reloads its own page. */
export function provideHandoff(key: keyof Parts, provider: Provider): void {
  providers[key] = provider;
}

/** Test helper: forget what was read, as a new page load would. */
export function resetHandoffForTests(): void {
  cache = null;
}

function empty(): void {
  if (window.name.startsWith(PREFIX)) window.name = "";
}

/**
 * The only way the session crosses a reload. Writes the handoff for this same address and reloads. Every reload the
 * code of Hokma makes goes through here; `reload` is only replaced by tests.
 */
export function reloadWithHandoff(reload: () => void = () => window.location.reload()): void {
  const parts: Parts = {};
  for (const key of ["t", "n"] as const) {
    const value = providers[key]?.() ?? null;
    if (value) parts[key] = value;
  }
  window.name = Object.keys(parts).length
    ? PREFIX + JSON.stringify({ o: window.location.origin, u: addressOfThisPage(), ...parts })
    : "";
  setTimeout(empty, TAKE_BACK_MS); // if the page stays, the copy does not
  reload();
}

if (typeof window !== "undefined") {
  window.addEventListener("pageshow", (event) => {
    // restored from the back/forward cache: memory is intact, so a copy in the name is not needed
    if ((event as PageTransitionEvent).persisted) empty();
  });
}
