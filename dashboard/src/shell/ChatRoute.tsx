import { useEffect } from "react";
import { chatHash, parseChatHash } from "./chatRoute";
import { conversationOf, isDraft } from "./tabsStorage";
import { useShell } from "./store";

/**
 * Keeps the address in step with the active panel tab and reads it back when it is edited.
 * Starting up from an address is done by `startupTabs`; this component covers what happens after.
 *
 * - draft tab: `#/chat/new`; conversation: `#/chat/<id>`; any other screen: no hash.
 * - always `replaceState`: moving between panel tabs does not fill the browser history.
 * - a hash this component does not own (`#t=...`, `#/connections`, ...) is never overwritten.
 */
export function ChatRoute() {
  const shell = useShell();
  const { tabs, activeKey, openScreen, newConversation } = shell;
  const active = tabs.find((tab) => tab.key === activeKey);
  const wanted = (() => {
    if (!active || active.screen !== "chat") return "";
    const id = conversationOf(active);
    if (id) return chatHash({ kind: "conversation", id });
    return isDraft(active) ? chatHash({ kind: "new" }) : "";
  })();

  useEffect(() => {
    const current = window.location.hash;
    if (current === wanted) return;
    if (current !== "" && parseChatHash(current) === null) return;
    window.history.replaceState(null, "", window.location.pathname + window.location.search + wanted);
  }, [wanted]);

  useEffect(() => {
    const follow = () => {
      const route = parseChatHash(window.location.hash);
      if (!route) return;
      if (route.kind === "conversation") {
        openScreen("chat", { conversation: route.id });
        return;
      }
      if (!(active && isDraft(active) && !active.conversation)) newConversation();
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [active, openScreen, newConversation]);

  return null;
}
