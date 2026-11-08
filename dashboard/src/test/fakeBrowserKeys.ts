/**
 * What jsdom does not have for the device key of the remembered session: a minimal IndexedDB (open, one object store,
 * get, put, delete, in transactions that complete) and the real WebCrypto of Node, so keys are real ECDSA P-256 keys.
 * The fake keeps the stored value by reference and counts nothing else. It exists only to run devicekey.ts and the
 * session module in unit tests.
 */
import { webcrypto } from "node:crypto";
import { vi } from "vitest";

type Handler = (() => void) | null;

interface FakeRequest<T> {
  result: T | undefined;
  error: Error | null;
  onsuccess: Handler;
  onerror: Handler;
}

interface FakeOpenRequest extends FakeRequest<unknown> {
  onupgradeneeded: Handler;
  onblocked: Handler;
}

export interface FakeIndexedDb {
  /** The records of each database: name -> store -> key -> value. */
  databases: Map<string, Map<string, Map<string, unknown>>>;
  /** When true, opening the database fails (what some private modes do). */
  failOpen: boolean;
  /** When true, writing a record fails (storage quota, private mode). */
  failWrite: boolean;
  open: (name: string, version?: number) => FakeOpenRequest;
}

const later = (work: () => void): void => {
  void Promise.resolve().then(work);
};

export function createFakeIndexedDb(): FakeIndexedDb {
  const fake: FakeIndexedDb = {
    databases: new Map(),
    failOpen: false,
    failWrite: false,
    open(name) {
      const request: FakeOpenRequest = { result: undefined, error: null, onsuccess: null, onerror: null, onupgradeneeded: null, onblocked: null };
      later(() => {
        if (fake.failOpen) {
          request.error = new Error("the browser refused to open the database");
          request.onerror?.();
          return;
        }
        const created = !fake.databases.has(name);
        const stores = fake.databases.get(name) ?? new Map<string, Map<string, unknown>>();
        fake.databases.set(name, stores);
        request.result = {
          objectStoreNames: { contains: (store: string) => stores.has(store) },
          createObjectStore: (store: string) => {
            stores.set(store, new Map());
          },
          close: () => undefined,
          transaction: (store: string) => {
            const records = stores.get(store);
            function run<T>(work: () => T, writes: boolean): FakeRequest<T> {
              const op: FakeRequest<T> = { result: undefined, error: null, onsuccess: null, onerror: null };
              later(() => {
                if (!records || (writes && fake.failWrite)) {
                  op.error = new Error("the request failed");
                  op.onerror?.();
                  return;
                }
                op.result = work();
                op.onsuccess?.();
                later(() => tx.oncomplete?.());
              });
              return op;
            }
            const tx = {
              error: null as Error | null,
              oncomplete: null as Handler,
              onerror: null as Handler,
              onabort: null as Handler,
              objectStore: () => ({
                get: (key: string) => run(() => records?.get(key), false),
                put: (value: unknown, key: string) =>
                  run(() => {
                    records?.set(key, value);
                    return key;
                  }, true),
                delete: (key: string) =>
                  run(() => {
                    records?.delete(key);
                    return undefined;
                  }, true),
              }),
            };
            return tx;
          },
        };
        if (created) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
  };
  return fake;
}

/** The record the app keeps, as the fake sees it: undefined when there is none. */
export function storedDeviceRecord(fake: FakeIndexedDb): unknown {
  return fake.databases.get("hokma-session")?.get("keys")?.get("device");
}

/** Gives the test a working IndexedDB and the WebCrypto of Node. Undo with `vi.unstubAllGlobals()`. */
export function installBrowserKeys(): FakeIndexedDb {
  const fake = createFakeIndexedDb();
  vi.stubGlobal("indexedDB", fake);
  vi.stubGlobal("crypto", webcrypto);
  return fake;
}
