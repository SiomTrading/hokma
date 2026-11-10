import { useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, Field, Glyph, Input, Loading, Modal, Pane, Pill, Row } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { launchTerminal } from "../terminal/store";
import { normalizeServerName, serverProblem } from "./linkFormats";
import type { RemoteServer, RemoteState } from "./types";


const TONE: Record<RemoteState, "ok" | "org" | "bad"> = { connected: "ok", needs_auth: "org", error: "bad", no_cli: "bad" };

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** Settings > Extensions: remote MCP servers with sign-in (name + address, then Authorize / Revoke). */
export function RemoteServers({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<{ servers: RemoteServer[] }>("/api/extensions/servers");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auth, setAuth] = useState<{ name: string; url: string } | null>(null);
  const [pasted, setPasted] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const problem = serverProblem(name, url);
  const valid = problem === null;

  const refresh = () => {
    list.reload();
    onChanged();
  };

  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    setBusy("add");
    setError(null);
    try {
      const cleanName = name.trim().replace(/[-_]+$/, "");
      await api.post("/api/extensions/servers", { name: cleanName, url: url.trim() });
      shell.toast({ tone: "ok", text: t("mcp.added", { name: cleanName }) });
      setName("");
      setUrl("");
      refresh();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  const authorize = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    try {
      const started = await api.post<{ auth_url: string | null; terminal_command?: string }>(`/api/extensions/servers/${server.name}/login`);
      if (!started.auth_url) {
        // Windows without a usable hidden console: the sign-in runs in Hokma's own Terminal, one click, no typing.
        if (!started.terminal_command) throw new Error(t("api.error"));
        if (!shell.layout.terminal) shell.toggleTerminal();
        launchTerminal({ command: started.terminal_command });
        shell.closeSettings();
        shell.toast({ tone: "ok", text: t("mcp.viaTerminal", { name: server.name }) });
        return;
      }
      setPasted("");
      setAuthError(null);
      setAuth({ name: server.name, url: started.auth_url });
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  const closeAuth = () => {
    if (auth) void api.post(`/api/extensions/servers/${auth.name}/login/cancel`).catch(() => undefined);
    setAuth(null);
    setAuthError(null);
    refresh();
  };

  const connect = async () => {
    if (!auth || !pasted.trim()) return;
    setBusy(auth.name);
    setAuthError(null);
    try {
      await api.post(`/api/extensions/servers/${auth.name}/login/code`, { pasted: pasted.trim() });
      shell.toast({ tone: "ok", text: t("mcp.connected", { name: auth.name }) });
      setAuth(null);
      refresh();
    } catch (cause) {
      setAuthError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
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
      {list.data?.servers.map((server) => (
        <Pane key={server.name} style={{ padding: 0 }}>
          <Row>
            <Glyph initial={server.name.slice(0, 1).toUpperCase()} size={34} />
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
              <span className="h2">{server.name}</span>
              <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{server.url}</span>
              {server.state === "error" && server.message ? <span className="p" style={{ fontSize: 12, color: "var(--bad)" }}>{server.message}</span> : null}
            </div>
            <Pill tone={TONE[server.state]}>{t(`mcp.state.${server.state}`)}</Pill>
            {server.state === "connected" ? (
              <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void revoke(server)}>{t("mcp.revoke")}</Button>
            ) : (
              <Button size="sm" variant="primary" disabled={busy !== null || server.state === "no_cli"} onClick={() => void authorize(server)}>
                {t(server.state === "error" ? "mcp.retry" : "mcp.authorize")}
              </Button>
            )}
          </Row>
        </Pane>
      ))}

      {auth ? (
        <Modal label={t("mcp.modal.title", { name: auth.name })} onClose={closeAuth} style={{ flexDirection: "column", width: "min(560px, 100%)", padding: 22, gap: 14 }}>
          <h2 className="h1" style={{ fontSize: 17 }}>{t("mcp.modal.title", { name: auth.name })}</h2>
          <ol className="p" style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6 }}>
            <li>{t("mcp.modal.step1")}</li>
            <li>{t("mcp.modal.step2")}</li>
            <li>{t("mcp.modal.step3")}</li>
          </ol>
          <div>
            <a className="btn" href={auth.url} target="_blank" rel="noopener noreferrer">{t("mcp.modal.open")}</a>
          </div>
          <Field label={t("mcp.modal.paste")} error={authError ?? undefined}>
            {(control) => <Input {...control} value={pasted} onChange={(event) => setPasted(event.target.value)} placeholder="http://localhost:.../callback?code=..." autoComplete="off" spellCheck={false} />}
          </Field>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={closeAuth}>{t("ext.confirm.cancel")}</Button>
            <Button variant="primary" disabled={busy !== null || !pasted.trim()} onClick={() => void connect()}>{t("mcp.modal.connect")}</Button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
