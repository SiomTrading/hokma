import { useId, useState } from "react";
import type { FormEvent } from "react";
import { Button, Dot, Field, Input, Pill, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { codeText, connectionErrorText, connectionsApi, isStoreUnavailable } from "./api";
import type { ConnectionCard, DataState, TestResult } from "./types";
import type { KeyPayload } from "./api";

type Busy = "save" | "remove" | "test" | "toggle" | null;

interface Props {
  card: ConnectionCard;
  storeAvailable: boolean;
  focused: boolean;
  onChange: (card: ConnectionCard) => void;
  onStoreMissing: () => void;
  onReload: () => void;
}

const STATE_TONE = { connected: "ok", reconnecting: "warn", error: "bad", not_connected: "default" } as const;

/** Labels of the secret fields: one per name the backend reports in `key_fields`. */
function fieldLabelKey(card: ConnectionCard, field: string): string {
  if (field === "key_id") return "connections.field.keyId";
  if (field === "secret") return "connections.field.secret";
  return card.id === "siomtrading_futures" ? "connections.field.token" : "connections.field.key";
}

export function ProviderCard({ card, storeAvailable, focused, onChange, onStoreMissing, onReload }: Props) {
  const { t } = useI18n();
  const formId = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);

  const fields = card.key_fields.length > 0 ? card.key_fields : ["key"];
  const filled = card.needs_key && fields.every((field) => (values[field] ?? "").trim() !== "");
  const dataLabel = (state: DataState) => (state === "live" ? t("connections.data.live") : t("connections.data.delayed"));
  const errorText = card.state === "error" ? codeText(card.error_code, card.error_message) : "";

  const fail = (cause: unknown) => {
    if (isStoreUnavailable(cause)) onStoreMissing();
    setError(connectionErrorText(cause));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!filled || busy) return;
    const body: KeyPayload = {};
    for (const field of fields) body[field as keyof KeyPayload] = (values[field] ?? "").trim();
    setBusy("save");
    setError(null);
    setTest(null);
    try {
      await connectionsApi.saveKey(card.id, body);
      setValues({}); // the key leaves the panel the moment it is saved
      onReload();
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("remove");
    setError(null);
    setTest(null);
    try {
      await connectionsApi.removeKey(card.id);
      onReload();
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const toggle = async (enabled: boolean) => {
    setBusy("toggle");
    setError(null);
    try {
      onChange(await connectionsApi.setEnabled(card.id, enabled));
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const runTest = async () => {
    setBusy("test");
    setError(null);
    setTest(null);
    try {
      setTest(await connectionsApi.test(card.id));
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const notes: Array<{ key: string; text: string; testId?: string }> = [];
  if (card.id === "alpaca") notes.push({ key: "iex", text: t("connections.note.alpaca") });
  if (card.id === "alpaca" || card.id === "finnhub") notes.push({ key: "limit", text: t("connections.note.limit", { max: card.max_symbols }) });
  if (card.premium) notes.push({ key: "premium", text: t("connections.note.premium") });
  if (!card.available) notes.push({ key: "unavailable", text: t("connections.note.unavailable"), testId: "conn-unavailable" });

  const keyStatus = card.key_state === "saved" ? t("connections.key.saved") : t("connections.key.missing");

  return (
    <article
      className={"pane conn-card" + (focused ? " focus" : "")}
      aria-label={card.name}
      data-testid={"connection-" + card.id}
      data-provider={card.id}
      data-focus={focused ? "true" : undefined}
    >
      <header className="conn-head">
        <span className="h2">{card.name}</span>
        <div className="conn-pills">
          <Pill tone={STATE_TONE[card.state]} data-testid="conn-state" data-state={card.state}>
            <Dot live={card.state === "connected"} />
            {card.state === "connected"
              ? t("connections.state.connected")
              : card.state === "reconnecting"
                ? t("connections.state.reconnecting")
                : card.state === "error"
                  ? t("connections.state.error")
                  : t("connections.state.notConnected")}
          </Pill>
          <Pill tone={card.data_state === "live" ? "acc" : "warn"} data-testid="conn-data" data-data-state={card.data_state}>
            {dataLabel(card.data_state)}
          </Pill>
        </div>
      </header>

      {errorText ? (
        <p className="conn-error" role="alert" data-testid="conn-error">
          {errorText}
        </p>
      ) : null}

      <div className="conn-line">
        <span className="p" data-testid="conn-symbols">
          {t("connections.symbols", { used: card.symbols_in_use, max: card.max_symbols })}
        </span>
        <Toggle label={t("connections.toggle", { name: card.name })} checked={card.enabled} disabled={busy === "toggle"} onChange={(next) => void toggle(next)} />
      </div>

      {notes.length > 0 ? (
        <ul className="conn-notes">
          {notes.map((note) => (
            <li key={note.key} className="p" data-testid={note.testId}>
              {note.text}
            </li>
          ))}
        </ul>
      ) : null}

      {card.needs_key ? (
        <form id={formId} className="conn-key" onSubmit={(event) => void save(event)} aria-label={t("connections.key.title", { name: card.name })}>
          <div className="conn-keystate">
            <span className="lbl">{t("connections.key.label")}</span>
            <Pill tone={card.key_state === "saved" ? "ok" : "warn"} data-testid="conn-key-state" data-key-state={card.key_state}>
              {keyStatus}
            </Pill>
          </div>
          <div className="conn-fields">
            {fields.map((field) => (
              <Field key={field} label={t(fieldLabelKey(card, field))}>
                {(control) => (
                  <Input
                    {...control}
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    data-field={field}
                    placeholder={card.key_state === "saved" ? t("connections.key.replace") : ""}
                    disabled={!storeAvailable || busy === "save"}
                    value={values[field] ?? ""}
                    onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))}
                  />
                )}
              </Field>
            ))}
          </div>
        </form>
      ) : null}

      <div className="conn-actions">
        {card.needs_key ? (
          <>
            <Button type="submit" form={formId} size="sm" variant="primary" disabled={!filled || !storeAvailable || busy !== null}>
              {busy === "save" ? t("connections.key.saving") : t("connections.key.save")}
            </Button>
            <Button size="sm" variant="danger" disabled={card.key_state !== "saved" || busy !== null} onClick={() => void remove()}>
              {t("connections.key.remove")}
            </Button>
          </>
        ) : null}
        <Button size="sm" disabled={busy !== null || !card.available} onClick={() => void runTest()}>
          {busy === "test" ? t("connections.test.running") : t("connections.test")}
        </Button>
      </div>

      {test ? (
        <p className={"conn-result" + (test.ok ? " ok" : " bad")} role="status" data-testid="conn-test-result" data-ok={test.ok ? "true" : "false"}>
          {test.ok
            ? test.latency_ms === null
              ? t("connections.test.okNoLatency", { data: dataLabel(test.data_state) })
              : t("connections.test.ok", { data: dataLabel(test.data_state), ms: Math.round(test.latency_ms) })
            : t("connections.test.fail", { reason: codeText(test.error_code, test.message) })}
        </p>
      ) : null}
      {error ? (
        <p className="conn-error" role="alert" data-testid="conn-action-error">
          {error}
        </p>
      ) : null}
    </article>
  );
}
