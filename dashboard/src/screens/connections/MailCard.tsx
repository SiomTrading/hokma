import { useCallback, useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { Button, Dot, Field, Input, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { connectionErrorText, mailApi } from "./api";
import type { MailState } from "./api";

/** The student's Gmail: address + app password, tested against Gmail by Hokma before it is kept. */
export function MailCard() {
  const { t } = useI18n();
  const formId = useId();
  const [mail, setMail] = useState<MailState | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    mailApi.state().then(setMail, (cause: unknown) => setError(connectionErrorText(cause)));
  }, []);
  useEffect(load, [load]);

  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await run();
      setEmail("");
      setPassword(""); // the password leaves the panel the moment it is saved
      load();
    } catch (cause) {
      setError(connectionErrorText(cause));
    } finally {
      setBusy(false);
    }
  };

  const connect = (event: FormEvent) => {
    event.preventDefault();
    if (filled && !busy) void act(() => mailApi.connect(email.trim(), password));
  };

  const filled = email.trim() !== "" && password.trim() !== "";
  const connected = mail?.connected === true;
  const storeOk = mail?.store_available !== false;

  return (
    <article className="pane conn-card" aria-label={t("mail.title")} data-testid="mail-card" data-mail={connected ? "connected" : "off"}>
      <header className="conn-head">
        <span className="h2">{t("mail.title")}</span>
        <Pill tone={connected ? "ok" : "default"} data-testid="mail-state">
          <Dot live={connected} />
          {connected ? t("connections.state.connected") : t("connections.state.notConnected")}
        </Pill>
      </header>

      {connected ? (
        <>
          <p className="p" data-testid="mail-account">
            {t("mail.connectedAs", { email: mail?.email ?? "" })}
          </p>
          <p className="p">{t("mail.connectedHelp")}</p>
          <div className="conn-actions">
            <Button size="sm" variant="danger" disabled={busy} onClick={() => void act(mailApi.disconnect)}>
              {t("mail.disconnect")}
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="p">{t("mail.help")}</p>
          {!storeOk ? (
            <p className="conn-error" role="alert">
              {t("connections.store.unavailable")}
            </p>
          ) : null}
          <form id={formId} className="conn-key" onSubmit={connect} aria-label={t("mail.connect")}>
            <div className="conn-fields">
              <Field label={t("mail.field.email")}>
                {(control) => (
                  <Input {...control} type="email" autoComplete="off" spellCheck={false} data-field="email" disabled={!storeOk || busy} value={email} onChange={(event) => setEmail(event.target.value)} />
                )}
              </Field>
              <Field label={t("mail.field.password")}>
                {(control) => (
                  <Input {...control} type="password" autoComplete="off" spellCheck={false} data-field="password" disabled={!storeOk || busy} value={password} onChange={(event) => setPassword(event.target.value)} />
                )}
              </Field>
            </div>
          </form>
          <div className="conn-actions">
            <Button type="submit" form={formId} size="sm" variant="primary" disabled={!filled || !storeOk || busy}>
              {busy ? t("mail.connecting") : t("mail.connect")}
            </Button>
            <a className="btn sm" href={mail?.apppasswords_url ?? "https://myaccount.google.com/apppasswords"} target="_blank" rel="noreferrer">
              {t("mail.openGoogle")}
            </a>
          </div>
        </>
      )}

      {error ? (
        <p className="conn-error" role="alert" data-testid="mail-error">
          {error}
        </p>
      ) : null}
    </article>
  );
}
