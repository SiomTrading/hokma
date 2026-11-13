import { useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, ErrorState, Field, Input, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";

export interface TelegramState {
  linked: boolean;
  connected: boolean;
  user_name: string;
}

export function telegramStatus(state: TelegramState): "linked" | "waiting" | "off" {
  return state.linked ? "linked" : state.connected ? "waiting" : "off";
}

const TONE = { linked: "ok", waiting: "warn", off: undefined } as const;

/** Settings > Telegram: paste the bot token, connect, approve the code the bot sends, unlink. */
export function TelegramSection() {
  const { t } = useI18n();
  const status = useApiQuery<TelegramState>("/api/telegram");
  const [saved, setSaved] = useState<TelegramState | null>(null);
  const [token, setToken] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (status.loading && !status.data) return <Loading />;
  if (status.error || !status.data) return <ErrorState message={status.error?.message} onRetry={status.reload} />;
  const state = saved ?? status.data;
  const current = telegramStatus(state);

  const run = async (action: () => Promise<TelegramState>) => {
    setBusy(true);
    setError(null);
    try {
      setSaved(await action());
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
  const approve = () =>
    run(async () => {
      const res = await api.post<{ user_name: string }>("/api/telegram/approve", { code: code.trim() });
      setCode("");
      return { linked: true, connected: state.connected, user_name: res.user_name };
    });
  const unlink = () =>
    run(async () => {
      await api.delete("/api/telegram");
      return { linked: false, connected: false, user_name: "" };
    });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Pane style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Pill tone={TONE[current]} data-testid="telegram-status">{t(`telegram.status.${current}`)}</Pill>
          {state.linked && state.user_name ? <span className="p">{t("telegram.linkedTo", { name: state.user_name })}</span> : null}
        </span>
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
        {current !== "off" ? <div><Button disabled={busy} onClick={() => void unlink()}>{t("telegram.unlink")}</Button></div> : null}
      </Pane>
      {error !== null ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
    </div>
  );
}
