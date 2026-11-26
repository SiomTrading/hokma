import { useSyncExternalStore } from "react";
import type { ConvActivity } from "./activity";
import { getHub } from "./hub";

/** The activity of a conversation, live: what the side panel and the line of the chat read. */
export function useActivity(conversationId: string | null): ConvActivity {
  const store = getHub().activity;
  return useSyncExternalStore(store.subscribe, () => store.get(conversationId));
}
