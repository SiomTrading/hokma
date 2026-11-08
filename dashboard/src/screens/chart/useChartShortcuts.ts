import { useEffect, useRef } from "react";
import type { RefObject } from "react";

export interface ChartShortcutHandlers {
  /** A letter or `^` opens the symbol search with it written; Ctrl+K / Cmd+K opens it empty (`""`). */
  onSearch: (initial: string) => void;
  /** A digit opens the interval box with it written. */
  onInterval: (initial: string) => void;
}

const LETTER = /^[A-Za-z^]$/;
const DIGIT = /^[0-9]$/;

/** Text fields, the chat composer and the terminal (xterm types into a hidden textarea) keep every key. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true'], .xterm, [role='textbox']")) return true;
  return target instanceof HTMLElement && target.isContentEditable;
}

/** Which shortcut a key press is, or null. Exported for the tests and for the help text. */
export function chartShortcutOf(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">): { kind: "search" | "interval"; initial: string } | null {
  if (event.altKey) return null;
  const mod = event.ctrlKey || event.metaKey;
  if (mod) {
    if (event.ctrlKey && event.metaKey) return null;
    return !event.shiftKey && event.key.toLowerCase() === "k" ? { kind: "search", initial: "" } : null; // Ctrl+C and the rest are not ours
  }
  if (LETTER.test(event.key)) return { kind: "search", initial: event.key };
  if (DIGIT.test(event.key)) return { kind: "interval", initial: event.key };
  return null;
}

/**
 * Chart keys (SPEC-001 addendum 6.4). They only act while the focus is inside the chart body (`containerRef`, which
 * has `tabIndex=0`) and never inside a text field. Handled keys stop there, so the shell does not also see them
 * (Ctrl+K inside the chart opens the symbol search, not the conversation search).
 */
export function useChartShortcuts(containerRef: RefObject<HTMLElement | null>, handlers: ChartShortcutHandlers): void {
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    // Listening on the document (capture) works even when the chart body mounts after this hook, and runs before the
    // shell's own shortcut listener.
    const onKeyDown = (event: KeyboardEvent) => {
      const node = containerRef.current;
      if (!node || !(event.target instanceof Node) || !node.contains(event.target)) return;
      if (event.defaultPrevented || event.isComposing || isTypingTarget(event.target)) return;
      const shortcut = chartShortcutOf(event);
      if (!shortcut) return;
      event.preventDefault();
      event.stopPropagation();
      if (shortcut.kind === "search") latest.current.onSearch(shortcut.initial);
      else latest.current.onInterval(shortcut.initial);
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [containerRef]);
}
