import { useEffect } from "react";
import { useShell } from "@/shell/useShell";
import { parseConnectionsHash, requestFocus } from "./focus";

/**
 * Opens Connections when the address is `#/connections`, `#/connections?class=equity` (the link of the chart) or
 * `#/connections?focus=schwab&login=1` (the button of the Schwab session notice).
 * The address is cleaned right after, so the same link works again the next time.
 */
export function HashRoute() {
  const { openScreen } = useShell();
  useEffect(() => {
    const follow = () => {
      const route = parseConnectionsHash(window.location.hash);
      if (!route) return;
      requestFocus(route.assetClass, { provider: route.provider, login: route.login });
      openScreen("connections");
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    };
    follow();
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [openScreen]);
  return null;
}
