/**
 * The owner confirmation of this tab (bug 1035).
 *
 * The session token can be read from the browser profile by anything that runs as the same user. The calls only the
 * owner may make (permission mode, Trash, restore points) therefore also carry `X-Hokma-Owner`: a random 128 bit
 * value this tab makes in memory and registers once with the engine (`POST /api/session/owner-nonce`). The engine
 * keeps only its hash, in memory.
 *
 * Where the value lives: in a variable of the page and nowhere else. No storage of the browser holds it (bug 1038).
 * To survive a reload it crosses through `window.name` (see handoff.ts), which is not persisted in the profile.
 */
import { provideHandoff, resetHandoffForTests, takeHandoff } from "./handoff";

const PATTERN = /^[0-9a-f]{32}$/;

const ONCE_PATTERN = /^[A-Za-z0-9_-]{22,64}$/;

let nonce: string | null = null;
/**
 * The one-time code of the start link (`#o=`). Only in this variable: never in storage, never kept after the
 * engine accepted the registration. It is what lets this tab (and not a process that only has the token) register.
 */
let once: string | null = null;

export function setOwnerOnce(value: string): void {
  if (ONCE_PATTERN.test(value)) once = value;
}

export function getOwnerOnce(): string | null {
  return once;
}

export function clearOwnerOnce(): void {
  once = null;
}

function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function takeStored(): string | null {
  const value = takeHandoff("n");
  return value && PATTERN.test(value) ? value : null;
}

export function getOwnerNonce(): string {
  if (nonce === null) nonce = takeStored() ?? makeNonce();
  return nonce;
}

/**
 * Replaces the confirmation with a new random one and returns it. Used when this page proves who it is to the engine
 * from scratch (entering with a code, or resuming with the device key): that proof carries a confirmation made now.
 */
export function renewOwnerNonce(): string {
  nonce = makeNonce();
  return nonce;
}

/** Test helper: forget the value as a new page load would. */
export function resetOwnerForTests(): void {
  nonce = null;
  once = null;
  resetHandoffForTests();
}

provideHandoff("n", () => nonce);
