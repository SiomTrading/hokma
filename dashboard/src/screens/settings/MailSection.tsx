import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api";
import { Button, ErrorState, Loading, Pane, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { mailApi } from "../connections/api";
import type { MailNotify } from "../connections/api";

/** Settings > Email: which notices also go to the student's own Gmail, and a test mail. */
export function MailSection() {
  const { t } = useI18n();
  const shell = useShell();
  const [state, setState] = useState<MailNotify | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null);

  const load = useCallback(() => {
    setFailed(false);
    mailApi.notify().then(setState, () => setFailed(true));
  }, []);
  useEffect(load, [load]);

  if (failed) return <ErrorState message={t("api.error")} onRetry={load} />;
  if (!state) return <Loading />;

  if (!state.connected) {
    return (
      <Pane style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }} data-testid="mail-notify-off">
        <p className="p">{t("mailNotify.notConnected")}</p>
        <div>
          <Button variant="primary" onClick={() => shell.openScreen("connections")}>{t("mailNotify.openConnections")}</Button>
        </div>
      </Pane>
    );
  }

  const run = async (action: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await action();
      if (done) setMessage({ text: done, bad: false });
    } catch (cause) {
      setMessage({ text: cause instanceof ApiError ? cause.message : t("api.error"), bad: true });
    } finally {
      setBusy(false);
    }
  };
  const save = (fallback: boolean, jobs: number[]) => run(async () => setState(await mailApi.saveNotify(fallback, jobs)));
  const chosen = state.jobs.filter((job) => job.mail).map((job) => job.id);
  const flip = (id: number, on: boolean) => save(state.fallback, on ? [...chosen, id] : chosen.filter((x) => x !== id));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Pane style={{ padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        <p className="p" data-testid="mail-notify-account">{t("mailNotify.intro", { email: state.email ?? "" })}</p>
        <div>
          <Button size="sm" disabled={busy} onClick={() => void run(mailApi.sendTest, t("mailNotify.testSent", { email: state.email ?? "" }))}>
            {t("mailNotify.test")}
          </Button>
        </div>
      </Pane>
      <Pane style={{ padding: "4px 0" }}>
        <Row style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <span className="h2">{t("mailNotify.fallback")}</span>
          <Toggle checked={state.fallback} label={t("mailNotify.fallback")} disabled={busy} onChange={(on) => void save(on, chosen)} />
        </Row>
        <p className="p" style={{ fontSize: 12, padding: "0 12px 8px" }}>{t("mailNotify.fallback.help")}</p>
      </Pane>
      <Pane style={{ padding: "4px 0" }}>
        <span className="h2" style={{ padding: "8px 12px", display: "block" }}>{t("mailNotify.routines")}</span>
        {state.jobs.length === 0 ? (
          <div style={{ padding: "0 12px 12px", display: "flex", flexDirection: "column", gap: 8 }} data-testid="mail-notify-empty">
            <p className="p">{t("mailNotify.noRoutines")}</p>
            <div>
              <Button size="sm" onClick={() => shell.openScreen("tasks")}>{t("mailNotify.openTasks")}</Button>
            </div>
          </div>
        ) : (
          state.jobs.map((job) => (
            <Row key={job.id} style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <span className="p">{job.name}</span>
              <Toggle checked={job.mail} label={t("mailNotify.routine", { name: job.name })} disabled={busy} onChange={(on) => void flip(job.id, on)} />
            </Row>
          ))
        )}
      </Pane>
      {message ? <p className="p" role={message.bad ? "alert" : "status"} style={message.bad ? { color: "var(--bad)" } : undefined} data-testid="mail-notify-message">{message.text}</p> : null}
    </div>
  );
}
