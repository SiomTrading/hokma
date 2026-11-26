import { useSyncExternalStore } from "react";

/**
 * What the side panel remembers outside any one screen: which conversation it follows (SPEC-013 13.2), which turn the
 * chat asked it to point at, and which sections are folded. A module store and not a shell event: the chat announces
 * its conversation while it mounts, before the panel has registered a listener, and an event sent then is lost.
 */

const SECTIONS_KEY = "hokma.rightPanel.sections";

export interface Followed {
  conversationId: string;
  /** "chat": the general chat or a chat tab. "desk": the chat of a desk (label is the desk name). */
  source: "chat" | "desk";
  label: string;
}

export interface PanelState {
  followed: Followed | null;
  /** The turn the chat line pointed at; `nonce` changes on every request, so the same turn can be pointed at twice. */
  focus: { turnId: string; nonce: number } | null;
  /** Section id to true when folded. Absent means open. */
  collapsed: Record<string, boolean>;
  /** Id of the last failed turn the person already saw: the rail stops showing red for it. */
  acked: string | null;
  /** Id of the last protection block the person already saw: the rail stops showing amber for it. */
  ackedBlock: string | null;
}

function readCollapsed(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(SECTIONS_KEY);
    const data: unknown = raw ? JSON.parse(raw) : null;
    if (!data || typeof data !== "object") return {};
    return Object.fromEntries(Object.entries(data).filter(([, value]) => typeof value === "boolean")) as Record<string, boolean>;
  } catch {
    return {};
  }
}

function writeCollapsed(value: Record<string, boolean>): void {
  try {
    window.localStorage.setItem(SECTIONS_KEY, JSON.stringify(value));
  } catch {
    /* no storage: the sections stay as they are for this run */
  }
}

let state: PanelState = { followed: null, focus: null, collapsed: readCollapsed(), acked: null, ackedBlock: null };
const listeners = new Set<() => void>();
let nonce = 0;

function set(next: PanelState): void {
  state = next;
  listeners.forEach((listener) => listener());
}

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const usePanelState = (): PanelState => useSyncExternalStore(subscribe, () => state);

/** The panel follows this conversation from now on (a chat tab or a desk chat took the focus). */
export function followConversation(next: Followed): void {
  const now = state.followed;
  if (now && now.conversationId === next.conversationId && now.source === next.source && now.label === next.label) return;
  set({ ...state, followed: next });
}

export function focusTurn(turnId: string): void {
  nonce += 1;
  set({ ...state, focus: { turnId, nonce } });
}

export function ackFailure(turnId: string): void {
  if (state.acked !== turnId) set({ ...state, acked: turnId });
}

export function ackBlock(blockId: string): void {
  if (state.ackedBlock !== blockId) set({ ...state, ackedBlock: blockId });
}

export function setCollapsed(id: string, folded: boolean): void {
  const collapsed = { ...state.collapsed, [id]: folded };
  writeCollapsed(collapsed);
  set({ ...state, collapsed });
}

/** Tests: forget everything, and read the storage again. */
export function resetPanelState(): void {
  nonce = 0;
  set({ followed: null, focus: null, collapsed: readCollapsed(), acked: null, ackedBlock: null });
}
