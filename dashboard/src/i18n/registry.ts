import { useCallback, useMemo, useSyncExternalStore } from "react";

export type Locale = "es" | "en";
export const LOCALES: readonly Locale[] = ["es", "en"];
export const DEFAULT_LOCALE: Locale = "es";
const STORAGE_KEY = "hokma.locale";

export interface MessageModule<K extends string = string> {
  es: Record<K, string>;
  en: Record<K, string>;
}

/** Declares a module of texts. Fails to compile when es and en differ in keys. */
export function defineMessages<K extends string>(messages: MessageModule<K>): MessageModule<K> {
  return messages;
}

const catalogs: Record<Locale, Record<string, string>> = { es: {}, en: {} };
const keyOwner = new Map<string, number>();
const registered = new WeakSet<object>();
let moduleCount = 0;
const listeners = new Set<() => void>();

/** Adds a module to the catalog. A key defined twice throws: namespaces must not collide. */
export function registerMessages(mod: MessageModule): void {
  if (registered.has(mod)) return;
  registered.add(mod);
  moduleCount += 1;
  for (const locale of LOCALES) {
    for (const [key, text] of Object.entries(mod[locale])) {
      if (locale === "es") {
        if (keyOwner.has(key)) throw new Error(`Duplicate i18n key: ${key}`);
        keyOwner.set(key, moduleCount);
      }
      catalogs[locale][key] = text;
    }
  }
  emit();
}

export function getCatalog(locale: Locale): Readonly<Record<string, string>> {
  return catalogs[locale];
}

function detectLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "es" || stored === "en") return stored;
  } catch {
    /* storage unavailable */
  }
  return DEFAULT_LOCALE;
}

let currentLocale: Locale = detectLocale();

function emit(): void {
  listeners.forEach((l) => l());
}

export function getLocale(): Locale {
  return currentLocale;
}

export function setLocale(locale: Locale): void {
  if (locale === currentLocale) return;
  currentLocale = locale;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    /* storage unavailable */
  }
  if (typeof document !== "undefined") document.documentElement.lang = locale;
  emit();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export type Params = Record<string, string | number>;

export function t(key: string, params?: Params, locale: Locale = currentLocale): string {
  const text = catalogs[locale][key] ?? catalogs[DEFAULT_LOCALE][key];
  if (text === undefined) return key;
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name: string) => (name in params ? String(params[name]) : whole));
}

export function useI18n(): { t: (key: string, params?: Params) => string; locale: Locale; setLocale: (l: Locale) => void } {
  const locale = useSyncExternalStore(subscribe, getLocale, getLocale);
  const tr = useCallback((key: string, params?: Params) => t(key, params, locale), [locale]);
  return useMemo(() => ({ t: tr, locale, setLocale }), [tr, locale]);
}
