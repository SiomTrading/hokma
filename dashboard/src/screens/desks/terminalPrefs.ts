/** What the Terminal of each desk remembers on this computer: the width of the chat and whether Positions is folded. */
import { canonicalInterval } from "../chart/intervals";

export const CHAT_WIDTH_MIN = 300;
export const CHAT_WIDTH_DEFAULT = 380;

const widthKey = (deskId: number) => `hokma.desk.${deskId}.chatWidth`;
const foldedKey = (deskId: number) => `hokma.desk.${deskId}.positionsFolded`;

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode or a full disk: the preference is simply not kept.
  }
}

const ASSET_INTERVALS_KEY = "hokma.terminal.intervals";
const ASSET_INTERVALS_MAX = 100;

function readAssetIntervals(): Record<string, string> {
  try {
    const value: unknown = JSON.parse(read(ASSET_INTERVALS_KEY) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const out: Record<string, string> = {};
    for (const [symbol, interval] of Object.entries(value)) {
      const canonical = typeof interval === "string" ? canonicalInterval(interval) : null;
      if (canonical) out[symbol] = canonical;
    }
    return out;
  } catch {
    return {};
  }
}

/** The interval the Terminal chart last used for this asset, or null when it has none. */
export function loadAssetInterval(symbol: string): string | null {
  return readAssetIntervals()[symbol] ?? null;
}

/** Remembers the interval of the asset; the oldest assets go past the cap. */
export function saveAssetInterval(symbol: string, interval: string): void {
  const canonical = canonicalInterval(interval);
  if (!canonical) return;
  const all = readAssetIntervals();
  delete all[symbol];
  all[symbol] = canonical;
  const keys = Object.keys(all);
  for (const key of keys.slice(0, Math.max(0, keys.length - ASSET_INTERVALS_MAX))) delete all[key];
  write(ASSET_INTERVALS_KEY, JSON.stringify(all));
}

/** The widest the chat may be: half of the screen, never below the minimum. */
export function chatWidthMax(): number {
  const screen = typeof window === "undefined" ? 1280 : window.innerWidth;
  return Math.max(CHAT_WIDTH_MIN, Math.floor(screen / 2));
}

export function clampChatWidth(width: number): number {
  return Math.min(chatWidthMax(), Math.max(CHAT_WIDTH_MIN, Math.round(width)));
}

export function loadChatWidth(deskId: number): number {
  const raw = Number(read(widthKey(deskId)));
  return Number.isFinite(raw) && raw > 0 ? clampChatWidth(raw) : clampChatWidth(CHAT_WIDTH_DEFAULT);
}

export function saveChatWidth(deskId: number, width: number): void {
  write(widthKey(deskId), String(clampChatWidth(width)));
}

export function loadFolded(deskId: number): boolean {
  return read(foldedKey(deskId)) === "1";
}

export function saveFolded(deskId: number, folded: boolean): void {
  write(foldedKey(deskId), folded ? "1" : "0");
}
