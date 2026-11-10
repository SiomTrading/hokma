/** Addresses of the chat: `#/chat/new` is a new draft, `#/chat/<id>` is a stored conversation. Pure, no React. */

export type ChatRoute = { kind: "new" } | { kind: "conversation"; id: string };

const CHAT_HASH = /^#\/chat\/(new|[0-9]{1,18})$/;

/** The chat address in `hash`, or null for anything else (other hashes have their own readers). */
export function parseChatHash(hash: string): ChatRoute | null {
  const match = CHAT_HASH.exec(hash);
  if (!match) return null;
  return match[1] === "new" ? { kind: "new" } : { kind: "conversation", id: match[1]! };
}

export function chatHash(route: ChatRoute): string {
  return route.kind === "new" ? "#/chat/new" : `#/chat/${route.id}`;
}
