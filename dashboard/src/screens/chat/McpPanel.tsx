import { useState } from "react";
import { useApiQuery } from "@/api";
import { Button, Glyph, Loading, Modal, Pane, Pill, Row } from "@/design";
import { useI18n } from "@/i18n";
import { storeTab } from "@/screens/system/extTab";
import type { ExtensionsData, RemoteServer, RemoteState } from "@/screens/system/types";
import { useRemoteSignIn } from "@/screens/system/useRemoteSignIn";
import { useShell } from "@/shell/useShell";

const TONE: Record<RemoteState, "ok" | "org" | "bad"> = { connected: "ok", needs_auth: "org", error: "bad" };

/**
 * The chat's /mcp command: Hokma's own panel, not Claude Code's interactive one. It reads the same extensions API as
 * Settings > Extensions > MCP (task 3152) and signs in through the same hook, so there is no second implementation;
 * it does not depend on the engine in use (task 3158).
 */
export function McpPanel({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const remote = useApiQuery<{ servers: RemoteServer[] }>("/api/extensions/servers");
  const all = useApiQuery<ExtensionsData>("/api/extensions");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const servers = remote.data?.servers ?? [];
  const local = (all.data?.servers ?? []).filter((s) => !s.remote);
  const reload = () => {
    remote.reload();
    all.reload();
  };
  const { waiting, connect, cancel, revoke } = useRemoteSignIn({ servers, reload, onChanged: () => shell.emit("extensions:changed"), setBusy, setError });

  const openSettings = () => {
    storeTab("mcp");
    onClose();
    shell.openSettings("extensions");
  };

  const loading = (remote.loading && !remote.data) || (all.loading && !all.data);
  const failed = remote.error || all.error;

  return (
    <Modal label={t("chat.mcp.title")} onClose={onClose} style={{ flexDirection: "column", width: "min(620px, 100%)", maxHeight: "85vh", overflow: "auto", padding: 22, gap: 12 }}>
      <h2 className="h1" style={{ fontSize: 17 }}>{t("chat.mcp.title")}</h2>
      {loading ? <Loading inline /> : null}
      {failed ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{t("chat.mcp.loadFailed")}</p> : null}
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}

      {!loading && !failed && servers.length === 0 && local.length === 0 ? <p className="p">{t("chat.mcp.none")}</p> : null}

      {servers.length > 0 ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 6 }} aria-label={t("chat.mcp.remote")}>
          <span className="lbl">{t("chat.mcp.remote")}</span>
          <Pane style={{ padding: "4px 0" }}>
            {servers.map((server) => {
              const waitingHere = waiting === server.name || server.pending;
              const canRevoke = server.auth !== false && (server.signed_in ?? server.state === "connected");
              return (
                <Row key={server.name}>
                  <Glyph initial={server.name.slice(0, 1).toUpperCase()} size={34} />
                  <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                    <span className="h2">{server.name}</span>
                    <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{server.url}</span>
                    {server.state === "error" && server.message ? <span className="p" style={{ fontSize: 12, color: "var(--bad)", overflowWrap: "anywhere" }}>{server.message}</span> : null}
                  </div>
                  <Pill tone={TONE[server.state]}>{t(`chat.mcp.state.${server.state}`)}</Pill>
                  {waitingHere ? (
                    <Button size="sm" variant="ghost" onClick={() => void cancel(server)}>{t("mcp.cancel")}</Button>
                  ) : (
                    <Button size="sm" variant={server.state === "connected" ? "ghost" : "primary"} disabled={busy !== null} aria-label={`${t(server.state === "needs_auth" ? "chat.mcp.connect" : "chat.mcp.reconnect")} ${server.name}`} onClick={() => void connect(server)}>
                      {t(server.state === "needs_auth" ? "chat.mcp.connect" : "chat.mcp.reconnect")}
                    </Button>
                  )}
                  {canRevoke ? (
                    <Button size="sm" variant="ghost" disabled={busy !== null} aria-label={`${t("chat.mcp.revoke")} ${server.name}`} onClick={() => void revoke(server)}>{t("chat.mcp.revoke")}</Button>
                  ) : null}
                </Row>
              );
            })}
          </Pane>
        </section>
      ) : null}

      <section style={{ display: "flex", flexDirection: "column", gap: 6 }} aria-label={t("chat.mcp.local")}>
        <span className="lbl">{t("chat.mcp.local")}</span>
        {local.length === 0 ? (
          <p className="p">{t("chat.mcp.noneLocal")}</p>
        ) : (
          <Pane style={{ padding: "4px 0" }}>
            {local.map((server) => (
              <Row key={server.id}>
                <Glyph initial={server.name.slice(0, 1).toUpperCase()} size={34} />
                <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                  <span className="h2">{server.name}</span>
                  <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{server.target}</span>
                </div>
                <Pill tone={server.enabled ? "ok" : "default"}>{t(server.enabled ? "chat.mcp.local.on" : "chat.mcp.local.off")}</Pill>
              </Row>
            ))}
          </Pane>
        )}
      </section>

      <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
        <Button variant="ghost" onClick={openSettings}>{t("chat.mcp.settings")}</Button>
        <Button variant="primary" onClick={onClose}>{t("chat.mcp.close")}</Button>
      </div>
    </Modal>
  );
}
