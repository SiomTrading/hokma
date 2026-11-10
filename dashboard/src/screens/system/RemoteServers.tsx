import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, Field, Glyph, Input, Loading, Pane, Pill, Row } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { normalizeServerName, serverProblem } from "./linkFormats";
import type { RemoteServer, RemoteState } from "./types";


const TONE: Record<RemoteState, "ok" | "org" | "bad"> = { connected: "ok", needs_auth: "org", error: "bad" };
const POLL_MS = 2000;

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** True inside the desktop app, whose window cannot open a browser tab by itself. */
function inDesktopApp(): boolean {
  return typeof window !== "undefined" && ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);
}

/**
 * Settings > Extensions: remote MCP servers. Add (name + address), then Connect opens the system browser on the
 * provider's sign-in page; when the browser comes back to Hokma the list turns to Connected by itself.
 */
export function RemoteServers({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<{ servers: RemoteServer[] }>("/api/extensions/servers");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState<string | null>(null);
  const seenPending = useRef(false);
  const problem = serverProblem(name, url);
  const valid = problem === null;
  const servers = list.data?.servers ?? [];

  const refresh = () => {
    list.reload();
    onChanged();
  };

  // While a sign-in is open in the browser, look again every two seconds and when the window gets the focus back.
  useEffect(() => {
    if (!waiting) return undefined;
    const timer = window.setInterval(() => list.reload(), POLL_MS);
    const onFocus = () => list.reload();
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

  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    setBusy("add");
    setError(null);
    try {
      const cleanName = name.trim().replace(/[-_]+$/, "");
      const added = await api.post<RemoteServer>("/api/extensions/servers", { name: cleanName, url: url.trim() });
      shell.toast({ tone: "ok", text: t(added.state === "connected" ? "mcp.addedOpen" : "mcp.added", { name: cleanName }) });
      setName("");
      setUrl("");
      refresh();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  const connect = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    // A tab opened now, inside the click, is not blocked; it gets its address once the server answers.
    const tab = inDesktopApp() ? null : window.open("about:blank", "_blank");
    try {
      const started = await api.post<{ auth_url: string | null }>(`/api/extensions/servers/${server.name}/connect`);
      if (!started.auth_url) {
        tab?.close();
        refresh();
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
      list.reload();
    } catch (cause) {
      tab?.close();
      setError(errorText(cause, t("api.error")));
      list.reload();
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (server: RemoteServer) => {
    setWaiting(null);
    await api.post(`/api/extensions/servers/${server.name}/connect/cancel`).catch(() => undefined);
    list.reload();
  };

  const revoke = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    try {
      await api.post(`/api/extensions/servers/${server.name}/logout`);
      shell.toast({ tone: "ok", text: t("mcp.revoked", { name: server.name }) });
      refresh();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  const actions = (server: RemoteServer) => {
    if (waiting === server.name || server.pending) {
      return (
        <>
          <span className="hint" role="status">{t("mcp.waiting")}</span>
          <Button size="sm" variant="ghost" onClick={() => void cancel(server)}>{t("mcp.cancel")}</Button>
        </>
      );
    }
    if (server.state === "connected") {
      return server.auth === false ? null : (
        <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void revoke(server)}>{t("mcp.revoke")}</Button>
      );
    }
    return (
      <Button size="sm" variant="primary" disabled={busy !== null} onClick={() => void connect(server)}>
        {t(server.state === "error" ? "mcp.retry" : "mcp.connect")}
      </Button>
    );
  };

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("mcp.title")}>
      <span className="lbl">{t("mcp.title")}</span>
      <Pane style={{ padding: 14 }}>
        <form onSubmit={(event) => void add(event)} style={{ display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("mcp.form")}>
          <Field label={t("mcp.name")} hint={t("mcp.nameHint")}>
            {(control) => <Input {...control} value={name} onChange={(event) => setName(normalizeServerName(event.target.value))} placeholder="lauz" autoComplete="off" spellCheck={false} />}
          </Field>
          <Field label={t("mcp.url")} hint={t("mcp.urlHint")}>
            {(control) => <Input {...control} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://mcp.lauz.io/mcp" autoComplete="off" spellCheck={false} />}
          </Field>
          <div>
            <Button type="submit" size="sm" variant="primary" disabled={!valid || busy !== null}>{t("mcp.add")}</Button>
            {problem ? <span className="hint" role="status" style={{ marginLeft: 10 }}>{t("mcp.why." + problem)}</span> : null}
          </div>
        </form>
      </Pane>
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
      {list.loading && !list.data ? <Loading inline /> : null}
      {servers.map((server) => (
        <Pane key={server.name} style={{ padding: 0 }}>
          <Row>
            <Glyph initial={server.name.slice(0, 1).toUpperCase()} size={34} />
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
              <span className="h2">{server.name}</span>
              <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{server.url}</span>
              {server.state === "error" && server.message ? <span className="p" style={{ fontSize: 12, color: "var(--bad)" }}>{server.message}</span> : null}
            </div>
            <Pill tone={TONE[server.state]}>{t(`mcp.state.${server.state}`)}</Pill>
            {actions(server)}
          </Row>
        </Pane>
      ))}
    </section>
  );
}
