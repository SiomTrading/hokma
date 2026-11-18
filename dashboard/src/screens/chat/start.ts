/**
 * First message handed to a new chat tab by another screen (for example "Create it with Hokma" in the money desks).
 * It lives in memory, keyed by the draft id of the tab, and is taken once: the person still reads it and presses send.
 */
const pending = new Map<string, string>();

export function setChatStart(draft: string, text: string): void {
  pending.set(draft, text);
}

/** Returns the text waiting for this draft and forgets it. */
export function takeChatStart(draft: string | null): string | null {
  if (!draft) return null;
  const text = pending.get(draft);
  if (text === undefined) return null;
  pending.delete(draft);
  return text;
}
