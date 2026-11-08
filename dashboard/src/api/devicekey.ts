/**
 * The device key of the remembered session (task 3063).
 *
 * The page proves it is the same browser as before with an ECDSA P-256 key pair made by WebCrypto. The private key is
 * created NOT extractable: its bytes cannot be read by any script, not even this one. The pair lives in IndexedDB
 * (database `hokma-session`, store `keys`, key `device`), which is the one use of browser storage Hokma allows itself.
 *
 * What is stored: only that non-extractable CryptoKey pair. Never the session token, the owner confirmation, a
 * one-time code or a cookie, in any storage.
 *
 * What this protects and what it does not: the key material lives in the browser profile. That stops another server
 * on this machine (it cannot sign for the engine's origin) and a cookie that was copied (the cookie alone is not
 * enough, a signature of a fresh challenge is also needed). It does not protect against someone who can read the disk
 * of the user: they can take the profile as a whole.
 *
 * `deviceKeySupported`, `saveDeviceKey` and `deleteDeviceKey` never throw. `loadDeviceKey` rejects when the store cannot
 * be read, so the caller can tell "no key" from "no storage".
 */
const DB_NAME = "hokma-session";
const DB_VERSION = 1;
const STORE = "keys";
const RECORD = "device";

export interface DevicePublicKey {
  /** X coordinate of the public key, 32 bytes in base64url. */
  x: string;
  /** Y coordinate of the public key, 32 bytes in base64url. */
  y: string;
}

export interface CreatedDeviceKey {
  pair: CryptoKeyPair;
  publicKey: DevicePublicKey;
}

const COORDINATE = /^[A-Za-z0-9_-]{43}$/;

/** true when this browser has both IndexedDB and WebCrypto, which is what remembering the session needs. */
export function deviceKeySupported(): boolean {
  try {
    return (
      typeof indexedDB !== "undefined" &&
      indexedDB !== null &&
      typeof crypto !== "undefined" &&
      typeof crypto.subtle?.generateKey === "function"
    );
  } catch {
    return false;
  }
}

export function toBase64Url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Makes a new pair. The private key is not extractable; the public key is exported as two base64url coordinates. */
export async function createDeviceKey(): Promise<CreatedDeviceKey> {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  if (typeof jwk.x !== "string" || typeof jwk.y !== "string" || !COORDINATE.test(jwk.x) || !COORDINATE.test(jwk.y)) {
    throw new Error("device key: unexpected public key");
  }
  return { pair, publicKey: { x: jwk.x, y: jwk.y } };
}

/** Signs `message` (UTF-8) with ECDSA and SHA-256. The result is the raw r||s form WebCrypto returns, in base64url. */
export async function signWithDeviceKey(pair: CryptoKeyPair, message: string): Promise<string> {
  const data = new TextEncoder().encode(message);
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, pair.privateKey, data);
  return toBase64Url(new Uint8Array(signature));
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, DB_VERSION);
    } catch (cause) {
      reject(cause);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("indexeddb: open failed"));
    request.onblocked = () => reject(new Error("indexeddb: open blocked"));
  });
}

async function inStore<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await openDatabase();
  try {
    return await new Promise<T>((resolve, reject) => {
      let result: T | undefined;
      const tx = db.transaction(STORE, mode);
      const request = run(tx.objectStore(STORE));
      request.onsuccess = () => {
        result = request.result;
      };
      request.onerror = () => reject(request.error ?? new Error("indexeddb: request failed"));
      tx.oncomplete = () => resolve(result as T);
      tx.onerror = () => reject(tx.error ?? new Error("indexeddb: transaction failed"));
      tx.onabort = () => reject(tx.error ?? new Error("indexeddb: transaction aborted"));
    });
  } finally {
    db.close();
  }
}

function isPair(value: unknown): value is CryptoKeyPair {
  if (value === null || typeof value !== "object") return false;
  const pair = value as { privateKey?: { type?: unknown; extractable?: unknown }; publicKey?: unknown };
  return pair.privateKey?.type === "private" && pair.privateKey.extractable === false && pair.publicKey !== undefined;
}

/** Keeps the pair. Resolves true when it is stored, false when the browser refused it. */
export async function saveDeviceKey(pair: CryptoKeyPair): Promise<boolean> {
  try {
    await inStore("readwrite", (store) => store.put(pair, RECORD));
    return true;
  } catch {
    return false;
  }
}

/** The stored pair, or null when there is none (or what is stored is not a usable non-extractable pair). Rejects when the store cannot be read. */
export async function loadDeviceKey(): Promise<CryptoKeyPair | null> {
  const value = await inStore<unknown>("readonly", (store) => store.get(RECORD));
  return isPair(value) ? value : null;
}

/** Removes the pair. Never throws: a store that cannot be reached has nothing the page can remove. */
export async function deleteDeviceKey(): Promise<void> {
  if (typeof indexedDB === "undefined" || indexedDB === null) return;
  try {
    await inStore("readwrite", (store) => store.delete(RECORD));
  } catch {
    /* nothing to remove, or the store is not reachable */
  }
}
