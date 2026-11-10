import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import type { RemoteServer } from "./types";

const POLL_MS = 2000;

export function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** True inside the desktop app, whose window cannot open a browser tab by itself. */
function inDesktopApp(): boolean {
  return typeof window !== "undefined" && ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);
}

interface Options {
  servers: RemoteServer[];
  reload: () => void;
  /** Something changed for the rest of the app (the servers list is already reloaded). */
  onChanged: () => void;
  setBusy: (name: string | null) => void;
  setError: (message: string | null) => void;
}

/**
 * Connect / cancel / revoke of a remote MCP server through the extensions API (task 3152), shared by Settings >
 * Extensions > MCP and the chat's /mcp panel (task 3158) so the sign-in has one implementation. Connect opens the
 * system browser on the provider's page; while it is open the list is read again every two seconds and when the
 * window gets the focus back, and the toast "connected" comes when the server turns Connected.
 */
export function useRemoteSignIn({ servers, reload, onChanged, setBusy, setError }: Options) {
  const { t } = useI18n();
  const shell = useShell();
  const [waiting, setWaiting] = useState<string | null>(null);
  const seenPending = useRef(false);

  useEffect(() => {
    if (!waiting) return undefined;
    const timer = window.setInterval(() => reload(), POLL_MS);
    const onFocus = () => reload();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [waiting]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!waiting) return;
    const server = servers.find((s) => s.name === waiting);
    if (!server) return;
    if (server.pending) seenPending.current = true;
    if (server.state === "connected") {
      setWaiting(null);
      shell.toast({ tone: "ok", text: t("mcp.connected", { name: server.name }) });
      onChanged();
    } else if (server.state === "error" || (seenPending.current && !server.pending)) {
      setWaiting(null);
    }
  }, [servers, waiting]); // eslint-disable-line react-hooks/exhaustive-deps

  const connect = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    // A tab opened now, inside the click, is not blocked; it gets its address once the server answers.
    const tab = inDesktopApp() ? null : window.open("about:blank", "_blank");
    try {
      const started = await api.post<{ auth_url: string | null }>(`/api/extensions/servers/${server.name}/connect`);
      if (!started.auth_url) {
        tab?.close();
        reload();
        onChanged();
        return;
      }
      if (tab) {
        tab.opener = null;
        tab.location.href = started.auth_url;
      } else {
        await api.post(`/api/extensions/servers/${server.name}/open`, { url: started.auth_url });
      }
      seenPending.current = false;
      setWaiting(server.name);
      reload();
    } catch (cause) {
      tab?.close();
      setError(errorText(cause, t("api.error")));
      reload();
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (server: RemoteServer) => {
    setWaiting(null);
    await api.post(`/api/extensions/servers/${server.name}/connect/cancel`).catch(() => undefined);
    reload();
  };

  const revoke = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    try {
      await api.post(`/api/extensions/servers/${server.name}/logout`);
      shell.toast({ tone: "ok", text: t("mcp.revoked", { name: server.name }) });
      reload();
      onChanged();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  return { waiting, setWaiting, connect, cancel, revoke };
}
