import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { codeText, connectionErrorText, connectionsApi, inDesktopApp, isStoreUnavailable, schwabApi } from "./api";
import { GuideFail, ProviderGuide } from "./ProviderGuide";
import type { ConnectionCard, DataState, TestResult } from "./types";

/** The address Schwab sends the browser back to. The student copies it into the app of Schwab; Hokma never listens on it. */
export const SCHWAB_CALLBACK = "https://127.0.0.1:8182";

type Step = 1 | 2 | 3;
type Busy = "save" | "login" | "finish" | null;

interface Props {
  card: ConnectionCard;
  /** 2 when the keys are already saved (the session notice and "Sign in with Schwab" of the row). */
  start: 1 | 2;
  /** "Back": to the list of providers, or closes the dialog when it did not start there. */
  onBack: () => void;
  onClose: () => void;
  onCard: (card: ConnectionCard) => void;
  onReload: () => void;
  onStoreMissing: () => void;
}

/** Schwab in three steps inside the "Add" dialog: your app's Client ID and Secret, sign in, test. */
export function SchwabSteps({ card, start, onBack, onClose, onCard, onReload, onStoreMissing }: Props) {
  const { t } = useI18n();
  const [step, setStep] = useState<Step>(start);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ card: ConnectionCard; test: TestResult } | null>(null);

  const filled = clientId.trim() !== "" && clientSecret.trim() !== "";
  const signedIn = card.added && (card.session?.state === "signed_in" || card.session?.state === "expiring");
  const dataLabel = (state: DataState) => (state === "live" ? t("connections.data.live") : t("connections.data.delayed"));

  const fail = (cause: unknown) => {
    if (isStoreUnavailable(cause)) onStoreMissing();
    setError(connectionErrorText(cause));
  };

  const saveNext = async (event: FormEvent) => {
    event.preventDefault();
    if (!filled || busy) return;
    setBusy("save");
    setError(null);
    try {
      await connectionsApi.saveKey(card.id, { client_id: clientId.trim(), client_secret: clientSecret.trim() });
      setClientId("");
      setClientSecret(""); // the keys leave the panel the moment they are saved
      onReload();
      setStep(2);
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const openLogin = async () => {
    if (busy) return;
    setBusy("login");
    setError(null);
    // A tab opened now, inside the click, is not blocked; it gets its address once the engine answers.
    const tab = inDesktopApp() ? null : window.open("about:blank", "_blank");
    try {
      const { auth_url: authUrl } = await schwabApi.loginStart();
      if (tab) {
        tab.opener = null;
        tab.location.href = authUrl;
      } else {
        await schwabApi.loginOpen(authUrl);
      }
    } catch (cause) {
      tab?.close();
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const connect = async (event: FormEvent) => {
    event.preventDefault();
    if (pasted.trim() === "" || busy) return;
    setBusy("finish");
    setError(null);
    setResult(null);
    setStep(3);
    try {
      const answer = await schwabApi.loginFinish(pasted.trim());
      setPasted("");
      onCard(answer.card);
      onReload();
      setResult(answer);
    } catch (cause) {
      setStep(2);
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const copy = () => {
    void navigator.clipboard?.writeText(SCHWAB_CALLBACK).catch(() => undefined);
  };

  const failed = result !== null && !result.test.ok;
  const account = (result?.card ?? card).session?.accounts?.[0]?.mask;

  return (
    <div className="conn-steps" data-testid="schwab-steps" data-step={step}>
      {step === 1 ? (
        <form className="conn-key" onSubmit={(event) => void saveNext(event)} aria-label={t("connections.schwab.step1")}>
          <h3 className="h2">{t("connections.schwab.step1")}</h3>
          <ProviderGuide id="schwab" foldable open />
          <p className="p">{t("connections.schwab.step1.help")}</p>
          <div className="conn-fields">
            <Field label={t("connections.field.clientId")}>
              {(control) => (
                <Input {...control} type="password" autoComplete="off" spellCheck={false} data-field="client_id" disabled={busy === "save"} value={clientId} onChange={(event) => setClientId(event.target.value)} />
              )}
            </Field>
            <Field label={t("connections.field.clientSecret")}>
              {(control) => (
                <Input {...control} type="password" autoComplete="off" spellCheck={false} data-field="client_secret" disabled={busy === "save"} value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} />
              )}
            </Field>
          </div>
          <div className="conn-line">
            <span className="p" data-testid="schwab-callback">
              {t("connections.schwab.callback", { url: SCHWAB_CALLBACK })}
            </span>
            <Button size="sm" onClick={copy}>
              {t("connections.schwab.copy")}
            </Button>
          </div>
          <div className="conn-actions conn-end">
            <Button onClick={onBack} disabled={busy !== null}>
              {t("connections.add.back")}
            </Button>
            <Button type="submit" variant="primary" disabled={!filled || busy !== null}>
              {t("connections.schwab.saveNext")}
            </Button>
          </div>
        </form>
      ) : null}

      {step === 2 ? (
        <div className="conn-key">
          <h3 className="h2">{t("connections.schwab.step2")}</h3>
          <ProviderGuide id="schwab" foldable open={false} />
          <ol className="conn-guide-steps p">
            <li>{t("connections.schwab.step2.a")}</li>
            <li>{t("connections.schwab.step2.b")}</li>
            <li>{t("connections.schwab.step2.c")}</li>
          </ol>
          {signedIn ? (
            <p className="p" data-testid="schwab-relogin">
              {t("connections.schwab.reloginNote")}
            </p>
          ) : null}
          <div className="conn-actions conn-end">
            <Button variant="primary" disabled={busy !== null} onClick={() => void openLogin()}>
              {t("connections.schwab.login")}
            </Button>
          </div>
          <form className="conn-key" onSubmit={(event) => void connect(event)} aria-label={t("connections.schwab.step2")}>
            <Field label={t("connections.schwab.paste")}>
              {(control) => (
                <Input {...control} type="text" autoComplete="off" spellCheck={false} data-field="redirect_url" disabled={busy === "finish"} value={pasted} onChange={(event) => setPasted(event.target.value)} />
              )}
            </Field>
            <div className="conn-actions conn-end">
              {start === 1 ? (
                <Button onClick={() => setStep(1)} disabled={busy !== null}>
                  {t("connections.add.back")}
                </Button>
              ) : (
                <Button onClick={onBack} disabled={busy !== null}>
                  {t("connections.add.back")}
                </Button>
              )}
              <Button type="submit" variant="primary" disabled={pasted.trim() === "" || busy !== null}>
                {t("connections.schwab.connect")}
              </Button>
            </div>
          </form>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="conn-key" role="status" aria-live="polite">
          <h3 className="h2">{t("connections.schwab.step3")}</h3>
          {result === null ? (
            <p className="p" data-testid="schwab-testing">
              {t("connections.add.testing", { name: card.name })}
            </p>
          ) : failed ? (
            <>
              <p className="conn-error" data-testid="schwab-test-failed">
                {t("connections.add.failed", { reason: codeText(result.test.error_code, result.test.message) })}
              </p>
              <div className="conn-actions conn-end">
                <Button onClick={() => setStep(2)}>{t("connections.add.back")}</Button>
              </div>
            </>
          ) : (
            <>
              <p className="p">
                <Pill tone="ok" data-testid="schwab-test-state">
                  {result.test.latency_ms === null
                    ? t("connections.test.okNoLatency", { data: dataLabel(result.test.data_state) })
                    : t("connections.test.ok", { data: dataLabel(result.test.data_state), ms: Math.round(result.test.latency_ms) })}
                </Pill>
              </p>
              {account ? (
                <p className="p mono" data-testid="schwab-account">
                  {t("connections.schwab.account", { mask: account })}
                </p>
              ) : null}
              <p className="p" data-testid="schwab-added">
                {t("connections.add.added", { name: card.name })}
              </p>
              <div className="conn-actions conn-end">
                <Button variant="primary" onClick={onClose}>
                  {t("connections.add.done")}
                </Button>
              </div>
            </>
          )}
        </div>
      ) : null}

      {error ? (
        <div className="conn-failure">
          <p className="conn-error" role="alert" data-testid="schwab-error">
            {error}
          </p>
          <GuideFail id="schwab" />
        </div>
      ) : null}
      {failed ? <GuideFail id="schwab" /> : null}
    </div>
  );
}
