import { useState } from "react";
import type { FormEvent } from "react";
import { api, useApiQuery } from "@/api";
import { Button, Field, Glyph, Input, Loading, Modal, Pane, Pill, Row } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { normalizeServerName, serverProblem, urlProblem } from "./linkFormats";
import type { RemoteServer, RemoteState } from "./types";
import { errorText, useRemoteSignIn } from "./useRemoteSignIn";

const TONE: Record<RemoteState, "ok" | "org" | "bad"> = { connected: "ok", needs_auth: "org", error: "bad" };

/**
 * Settings > Extensions: remote MCP servers. Add (name + address), then Connect opens the system browser on the
 * provider's sign-in page; when the browser comes back to Hokma the list turns to Connected by itself.
 * Every card can be removed, its address edited and its access revoked (task 3152). The engine checks an address
 * with an MCP `initialize` before saving it, so a web page is refused with its reason instead of showing Connected.
 */
export function RemoteServers({ onChanged }: { onChanged: () => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<{ servers: RemoteServer[] }>("/api/extensions/servers");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [removing, setRemoving] = useState<RemoteServer | null>(null);
  const servers = list.data?.servers ?? [];
  const problem = serverProblem(name, url, servers.map((s) => s.name));
  const valid = problem === null;
  const draftProblem = urlProblem(draft);

  const refresh = () => {
    list.reload();
    onChanged();
  };

  const { waiting, setWaiting, connect, cancel, revoke } = useRemoteSignIn({ servers, reload: list.reload, onChanged, setBusy, setError });

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

  const startEdit = (server: RemoteServer) => {
    setError(null);
    setDraft(server.url);
    setEditing(server.name);
  };

  const saveEdit = async (event: FormEvent, server: RemoteServer) => {
    event.preventDefault();
    if (draftProblem) return;
    setBusy(server.name);
    setError(null);
    try {
      await api.put<RemoteServer>(`/api/extensions/servers/${server.name}`, { url: draft.trim() });
      shell.toast({ tone: "ok", text: t("mcp.saved", { name: server.name }) });
      setEditing(null);
      refresh();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (server: RemoteServer) => {
    setBusy(server.name);
    setError(null);
    try {
      await api.delete(`/api/extensions/servers/${server.name}`);
      shell.toast({ tone: "ok", text: t("mcp.removed", { name: server.name }) });
      if (waiting === server.name) setWaiting(null);
      if (editing === server.name) setEditing(null);
      refresh();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
      setRemoving(null);
    }
  };

  const primary = (server: RemoteServer) => {
    if (waiting === server.name || server.pending) {
      return (
        <>
          <span className="hint" role="status">{t("mcp.waiting")}</span>
          <Button size="sm" variant="ghost" onClick={() => void cancel(server)}>{t("mcp.cancel")}</Button>
        </>
      );
    }
    if (server.state === "connected") return null;
    return (
      <Button size="sm" variant="primary" disabled={busy !== null} onClick={() => void connect(server)}>
        {t(server.state === "error" ? "mcp.retry" : "mcp.connect")}
      </Button>
    );
  };

  const actions = (server: RemoteServer) => {
    const canRevoke = server.auth !== false && (server.signed_in ?? server.state === "connected");
    return (
      <>
        {primary(server)}
        {canRevoke ? (
          <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void revoke(server)}>{t("mcp.revoke")}</Button>
        ) : null}
        <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => startEdit(server)}>{t("mcp.edit")}</Button>
        <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setRemoving(server)}>{t("mcp.remove")}</Button>
      </>
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
            {problem ? <span className="hint" role="status" style={{ marginLeft: 10 }}>{t("mcp.why." + problem, { name: name.trim().replace(/[-_]+$/, "") })}</span> : null}
            {busy === "add" ? <span className="hint" role="status" style={{ marginLeft: 10 }}>{t("mcp.checking")}</span> : null}
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
          {editing === server.name ? (
            <form onSubmit={(event) => void saveEdit(event, server)} style={{ display: "flex", flexDirection: "column", gap: 8, padding: "0 14px 14px" }} aria-label={`${t("mcp.edit")}: ${server.name}`}>
              <Field label={t("mcp.editLabel", { name: server.name })} hint={t("mcp.urlHint")}>
                {(control) => <Input {...control} value={draft} onChange={(event) => setDraft(event.target.value)} autoComplete="off" spellCheck={false} />}
              </Field>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <Button type="submit" size="sm" variant="primary" disabled={draftProblem !== null || busy !== null}>{t("mcp.save")}</Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>{t("mcp.cancel")}</Button>
                {draftProblem ? <span className="hint" role="status">{t("mcp.why." + draftProblem)}</span> : null}
                {busy === server.name ? <span className="hint" role="status">{t("mcp.checking")}</span> : null}
              </div>
            </form>
          ) : null}
        </Pane>
      ))}
      {removing ? (
        <Modal label={t("mcp.confirmRemove", { name: removing.name })} onClose={() => setRemoving(null)} style={{ flexDirection: "column", width: "min(520px, 100%)", padding: 22, gap: 14 }}>
          <h2 className="h1" style={{ fontSize: 17 }}>{t("mcp.confirmRemove", { name: removing.name })}</h2>
          <p className="p">{t("mcp.confirmRemoveBody")}</p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setRemoving(null)}>{t("ext.confirm.cancel")}</Button>
            <Button variant="primary" disabled={busy !== null} onClick={() => void remove(removing)}>{t("mcp.remove")}</Button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
