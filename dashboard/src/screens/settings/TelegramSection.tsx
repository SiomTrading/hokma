import { useEffect, useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, ErrorState, Field, Input, Loading, Modal, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";

export interface TelegramState {
  linked: boolean;
  connected: boolean;
  user_name: string;
  group_linked?: boolean;
  avisos?: "ok" | "no_permission" | "unreachable" | "failed" | null;
  /** Bug 1170: what Telegram answered when the Avisos topic failed (code, error class and a translated message). */
  permission_mode?: "ask" | "auto";
  avisos_detail?: { code: number | null; error: string; message: string } | null;
  outbox?: { pending: number; last_error: string | null };
  connect_error?: string | null;
}

/** `offline`: the link exists but the bot is not up (no network, Telegram blocked, token revoked). */
export function telegramStatus(state: TelegramState): "linked" | "offline" | "waiting" | "off" {
  if (state.linked) return state.connected ? "linked" : "offline";
  return state.connected ? "waiting" : "off";
}

type Confirming = "unlink" | "unlinkGroup" | null;

function ConfirmUnlink({ kind, busy, onCancel, onConfirm }: { kind: Exclude<Confirming, null>; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const { t } = useI18n();
  const prefix = kind === "unlink" ? "telegram.unlink" : "telegram.group.unlink";
  return (
    <Modal label={t(`${prefix}.title`)} onClose={onCancel} style={{ width: "min(440px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{t(`${prefix}.title`)}</h2>
      <p className="p">{t(`${prefix}.body`)}</p>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onCancel}>{t("telegram.cancel")}</Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>{t(prefix)}</Button>
      </div>
    </Modal>
  );
}

const TONE = { linked: "ok", offline: "warn", waiting: "warn", off: undefined } as const;
const RECHECK_MS = 10000;

/** Settings > Telegram: paste the bot token, connect, approve the code the bot sends, unlink. */
export function TelegramSection() {
  const { t } = useI18n();
  const status = useApiQuery<TelegramState>("/api/telegram");
  const [saved, setSaved] = useState<TelegramState | null>(null);
  const [token, setToken] = useState("");
  const [code, setCode] = useState("");
  const [groupCode, setGroupCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<Confirming>(null);
  const { reload } = status;

  // What the server says wins: a result kept from a click lasts only until the next answer arrives.
  useEffect(() => setSaved(null), [status.data]);
  const watching = status.data?.linked === true && (!status.data.connected || (status.data.outbox?.pending ?? 0) > 0);
  useEffect(() => {
    if (!watching) return;
    const timer = setInterval(reload, RECHECK_MS);
    return () => clearInterval(timer);
  }, [watching, reload]);

  if (status.loading && !status.data) return <Loading />;
  if (status.error || !status.data) return <ErrorState message={status.error?.message} onRetry={status.reload} />;
  const state = saved ?? status.data;
  const current = telegramStatus(state);

  const run = async (action: () => Promise<TelegramState>) => {
    setBusy(true);
    setError(null);
    try {
      setSaved(await action());
      reload();
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
    } finally {
      setBusy(false);
    }
  };

  const connect = () =>
    run(async () => {
      const res = await api.put<{ connected: boolean }>("/api/telegram/token", { token: token.trim() });
      setToken("");
      return { ...state, connected: res.connected };
    });
  const reconnect = () =>
    run(async () => {
      const res = await api.post<{ connected: boolean }>("/api/telegram/reconnect");
      return { ...state, connected: res.connected };
    });
  const approve = () =>
    run(async () => {
      const res = await api.post<{ user_name: string }>("/api/telegram/approve", { code: code.trim() });
      setCode("");
      return { linked: true, connected: state.connected, user_name: res.user_name };
    });
  const unlink = () =>
    run(async () => {
      await api.delete("/api/telegram");
      return { linked: false, connected: false, user_name: "", group_linked: false, avisos: null };
    });
  const approveGroup = () =>
    run(async () => {
      const res = await api.post<{ avisos?: TelegramState["avisos"]; avisos_detail?: TelegramState["avisos_detail"] }>("/api/telegram/group/approve", { code: groupCode.trim() });
      setGroupCode("");
      return { ...state, group_linked: true, avisos: res.avisos ?? null, avisos_detail: res.avisos_detail ?? null };
    });
  const unlinkGroup = () =>
    run(async () => {
      await api.delete("/api/telegram/group");
      return { ...state, group_linked: false, avisos: null, avisos_detail: null };
    });
  const retryAvisos = () =>
    run(async () => {
      const res = await api.post<{ avisos?: TelegramState["avisos"]; avisos_detail?: TelegramState["avisos_detail"] }>("/api/telegram/group/avisos");
      return { ...state, avisos: res.avisos ?? null, avisos_detail: res.avisos_detail ?? null };
    });
  const confirmed = () => {
    const kind = confirming;
    setConfirming(null);
    if (kind === "unlink") void unlink();
    if (kind === "unlinkGroup") void unlinkGroup();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Pane style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Pill tone={TONE[current]} data-testid="telegram-status">{t(`telegram.status.${current}`)}</Pill>
          {state.linked && state.user_name ? <span className="p">{t("telegram.linkedTo", { name: state.user_name })}</span> : null}
        </span>
        {state.linked && state.permission_mode ? (
          <p className="p" data-testid="telegram-mode">
            {t("telegram.mode.label", { mode: t(`permissions.mode.${state.permission_mode}`) })}
            {state.permission_mode === "auto" ? ` ${t("telegram.mode.auto_hint")}` : ` ${t("telegram.mode.ask_hint")}`}
          </p>
        ) : null}
        {current === "offline" ? (
          <>
            <p className="p" role="status">{t("telegram.offline.hint")}</p>
            {state.connect_error ? <p className="p" style={{ fontSize: 12, color: "var(--t3)" }}>{state.connect_error}</p> : null}
            <div><Button variant="primary" disabled={busy} onClick={() => void reconnect()}>{t("telegram.reconnect")}</Button></div>
          </>
        ) : null}
        {state.outbox && state.outbox.pending > 0 ? (
          <p className="p" data-testid="telegram-outbox" style={{ color: "var(--bad)" }}>
            {t("telegram.outbox.pending", { count: state.outbox.pending })}
            {state.outbox.last_error ? ` ${t("telegram.outbox.error", { error: state.outbox.last_error })}` : ""}
          </p>
        ) : null}
        {current === "off" ? (
          <>
            <Field label={t("telegram.token")} hint={t("telegram.token.hint")}>
              {(control) => <Input {...control} type="password" autoComplete="off" value={token} maxLength={100} onChange={(e) => setToken(e.target.value)} />}
            </Field>
            <div><Button variant="primary" disabled={busy || token.trim() === ""} onClick={() => void connect()}>{t("telegram.connect")}</Button></div>
          </>
        ) : null}
        {current === "waiting" ? (
          <>
            <p className="p">{t("telegram.instruction")}</p>
            <Field label={t("telegram.code")}>
              {(control) => <Input {...control} autoComplete="off" value={code} maxLength={16} onChange={(e) => setCode(e.target.value)} />}
            </Field>
            <div><Button variant="primary" disabled={busy || code.trim() === ""} onClick={() => void approve()}>{t("telegram.approve")}</Button></div>
          </>
        ) : null}
        {current !== "off" ? <div><Button disabled={busy} onClick={() => setConfirming("unlink")}>{t("telegram.unlink")}</Button></div> : null}
      </Pane>
      <Pane style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <strong className="p">{t("telegram.group.title")}</strong>
          {state.group_linked ? <Pill tone="ok" data-testid="telegram-group-status">{t("telegram.group.linked")}</Pill> : null}
          {state.group_linked && state.avisos ? (
            <Pill tone={state.avisos === "ok" ? "ok" : "warn"} data-testid="telegram-group-avisos">{t(`telegram.group.avisos.${state.avisos}`)}</Pill>
          ) : null}
        </span>
        {!state.linked ? <p className="p">{t("telegram.group.needsLink")}</p> : null}
        {state.linked && !state.group_linked ? (
          <>
            <ol className="p" style={{ margin: 0, paddingLeft: 20 }}>
              {[1, 2, 3, 4].map((n) => <li key={n}>{t(`telegram.group.step${n}`)}</li>)}
            </ol>
            <Field label={t("telegram.group.code")}>
              {(control) => <Input {...control} autoComplete="off" value={groupCode} maxLength={16} onChange={(e) => setGroupCode(e.target.value)} />}
            </Field>
            <div><Button variant="primary" disabled={busy || groupCode.trim() === ""} onClick={() => void approveGroup()}>{t("telegram.group.approve")}</Button></div>
          </>
        ) : null}
        {state.group_linked && (state.avisos === "no_permission" || state.avisos === "unreachable" || state.avisos === "failed") ? (
          <>
            <p className="p" role="status">{t(`telegram.avisos.hint.${state.avisos}`)}</p>
            {state.avisos_detail ? (
              <p className="p" data-testid="telegram-avisos-detail">
                {state.avisos_detail.code ? `${t("telegram.avisos.code", { code: state.avisos_detail.code })} ` : ""}{state.avisos_detail.message}
              </p>
            ) : null}
            <div><Button variant="primary" disabled={busy} onClick={() => void retryAvisos()}>{t("telegram.avisos.retry")}</Button></div>
          </>
        ) : null}
        {state.group_linked ? <div><Button disabled={busy} onClick={() => setConfirming("unlinkGroup")}>{t("telegram.group.unlink")}</Button></div> : null}
      </Pane>
      {confirming ? <ConfirmUnlink kind={confirming} busy={busy} onCancel={() => setConfirming(null)} onConfirm={confirmed} /> : null}
      {error !== null ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
    </div>
  );
}
