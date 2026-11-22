import { useId, useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, IconButton, Input, Modal, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { codeText, connectionErrorText, connectionsApi, isStoreUnavailable } from "./api";
import type { KeyPayload, MailState } from "./api";
import { MailCard } from "./MailCard";
import { GuideFail, ProviderGuide } from "./ProviderGuide";
import { SchwabSteps } from "./SchwabSteps";
import type { AddAnswer, ConnectionCard, DataState } from "./types";

/** Gmail is not a provider of the catalog: it is listed with them and has its own card. */
export const MAIL_ID = "mail";
/** The order of the list of 4.2 (pantalla 2). A provider that is not here goes after them. */
const LIST_ORDER = ["schwab", "alpaca", "finnhub", "siomtrading_futures", MAIL_ID, "coinbase"];

/** What the dialog shows: the list, the form of one provider, or its guide without fields. */
export type DialogView =
  | { view: "list" }
  | { view: "form"; id: string; mode: "add" | "change"; step?: 1 | 2 }
  | { view: "guide"; id: string };

interface Props {
  start: DialogView;
  cards: ConnectionCard[];
  mail: MailState | null;
  onClose: () => void;
  onCard: (card: ConnectionCard) => void;
  onReload: () => void;
  onMailChange: () => void;
  onStoreMissing: () => void;
}

/** Labels of the secret fields: one per name the backend reports in `key_fields`. */
function fieldLabelKey(card: ConnectionCard, field: string): string {
  if (field === "key_id") return "connections.field.keyId";
  if (field === "secret") return "connections.field.secret";
  if (field === "client_id") return "connections.field.clientId";
  if (field === "client_secret") return "connections.field.clientSecret";
  return card.id === "siomtrading_futures" ? "connections.field.token" : "connections.field.key";
}

/** "What it gives" line of a provider of the list. A provider without a line shows none. */
export function whatText(t: (key: string) => string, id: string): string {
  const key = "connections.add.what." + id;
  const text = t(key);
  return text === key ? "" : text;
}

/** "Add a connection": the list of what can be connected, and the form of the one the student picks. */
export function AddConnectionDialog({ start, cards, mail, onClose, onCard, onReload, onMailChange, onStoreMissing }: Props) {
  const { t } = useI18n();
  const [view, setView] = useState<DialogView>(start);
  const byId = new Map(cards.map((card) => [card.id, card]));
  const card = view.view === "list" ? undefined : byId.get(view.id);
  const title =
    view.view === "list"
      ? t("connections.add.title")
      : view.view === "guide"
        ? t("connections.row.guide") + " · " + (card?.name ?? t("mail.title"))
        : view.mode === "change"
          ? t("connections.row.changeKey") + " · " + (card?.name ?? t("mail.title"))
          : t("connections.add.formTitle", { name: card?.name ?? t("mail.title") });
  const back = start.view === "list" ? () => setView({ view: "list" }) : onClose;

  const rows: Array<{ id: string; name: string; added: boolean }> = [
    ...cards.map((c) => ({ id: c.id, name: c.name, added: c.added })),
    { id: MAIL_ID, name: t("mail.title"), added: mail?.connected === true },
  ].sort((a, b) => {
    const rank = (id: string) => (LIST_ORDER.includes(id) ? LIST_ORDER.indexOf(id) : LIST_ORDER.length);
    return rank(a.id) - rank(b.id);
  });

  return (
    <Modal label={title} onClose={onClose} className="conn-dialog">
      <div className="conn-dialog-body">
        <header className="conn-dialog-head">
          <h2 className="h1">{title}</h2>
          <IconButton icon="close" label={t("connections.add.close")} onClick={onClose} />
        </header>

        {view.view === "list" ? (
          <div className="conn-add-list" data-testid="add-list">
            <p className="p">{t("connections.add.pick")}</p>
            <ul className="conn-list">
              {rows.map((row) => {
                const labelId = "add-name-" + row.id;
                const what = whatText(t, row.id);
                return (
                  <li key={row.id} className="conn-row" data-testid={"add-" + row.id}>
                    <div className="conn-row-main">
                      <span className="h2" id={labelId}>
                        {row.name}
                      </span>
                      {what ? <span className="p conn-what">{what}</span> : null}
                    </div>
                    {row.added ? (
                      <Pill tone="ok">{t("connections.add.already")}</Pill>
                    ) : (
                      <Button size="sm" variant="primary" aria-describedby={labelId} onClick={() => setView({ view: "form", id: row.id, mode: "add" })}>
                        {t("connections.add.choose")}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
            <p className="p conn-note">{t("connections.add.fallbackNote")}</p>
          </div>
        ) : view.view === "guide" ? (
          <>
            <ProviderGuide id={view.id} />
            <div className="conn-actions conn-end">
              <Button onClick={onClose}>{t("connections.add.close")}</Button>
            </div>
          </>
        ) : view.id === MAIL_ID ? (
          <>
            <ProviderGuide id={MAIL_ID} foldable open />
            <MailCard showHelp={false} onChange={onMailChange} />
            <div className="conn-actions conn-end">
              <Button onClick={back}>{t("connections.add.back")}</Button>
              {mail?.connected ? (
                <Button variant="primary" onClick={onClose}>
                  {t("connections.add.done")}
                </Button>
              ) : null}
            </div>
          </>
        ) : card === undefined ? null : card.auth === "oauth" ? (
          <SchwabSteps key={card.id} card={card} start={view.step ?? 1} onBack={back} onClose={onClose} onCard={onCard} onReload={onReload} onStoreMissing={onStoreMissing} />
        ) : (
          <KeyForm card={card} onBack={back} onClose={onClose} onCard={onCard} onReload={onReload} onStoreMissing={onStoreMissing} />
        )}
      </div>
    </Modal>
  );
}

interface FormProps {
  card: ConnectionCard;
  onBack: () => void;
  onClose: () => void;
  onCard: (card: ConnectionCard) => void;
  onReload: () => void;
  onStoreMissing: () => void;
}

/** Form of a provider with a simple key, or of one with no key (Coinbase): "Test and add" in a single step. */
function KeyForm({ card, onBack, onClose, onCard, onReload, onStoreMissing }: FormProps) {
  const { t } = useI18n();
  const formId = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<AddAnswer | null>(null);

  const fields = card.needs_key ? (card.key_fields.length > 0 ? card.key_fields : ["key"]) : [];
  const filled = fields.every((field) => (values[field] ?? "").trim() !== "");
  const dataLabel = (state: DataState) => (state === "live" ? t("connections.data.live") : t("connections.data.delayed"));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!filled || testing || !card.available) return;
    const body: KeyPayload = {};
    for (const field of fields) body[field as keyof KeyPayload] = (values[field] ?? "").trim();
    setTesting(true);
    setError(null);
    try {
      const answer = await connectionsApi.add(card.id, body);
      onCard(answer.card);
      if (answer.added) {
        setValues({}); // the key leaves the panel the moment it is kept
        setDone(answer);
        onReload();
      } else {
        setError(t("connections.add.failed", { reason: codeText(answer.test.error_code, answer.test.message) }));
      }
    } catch (cause) {
      if (isStoreUnavailable(cause)) onStoreMissing();
      setError(t("connections.add.failed", { reason: connectionErrorText(cause) }));
    } finally {
      setTesting(false);
    }
  };

  if (done) {
    const test = done.test;
    return (
      <div className="conn-key" role="status" data-testid="add-done">
        <p className="p">
          <Pill tone="ok">
            {test.latency_ms === null
              ? t("connections.test.okNoLatency", { data: dataLabel(test.data_state) })
              : t("connections.test.ok", { data: dataLabel(test.data_state), ms: Math.round(test.latency_ms) })}
          </Pill>
        </p>
        <p className="p">{t("connections.add.added", { name: card.name })}</p>
        {card.id === "alpaca" ? (
          <p className="p" role="alert" data-testid="alpaca-notice">
            {t("connections.alpaca.notice")}
          </p>
        ) : null}
        <div className="conn-actions conn-end">
          <Button variant="primary" onClick={onClose}>
            {card.id === "alpaca" ? t("connections.alpaca.ok") : t("connections.add.done")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form id={formId} className="conn-key" onSubmit={(event) => void submit(event)} aria-label={t("connections.add.formTitle", { name: card.name })}>
      <ProviderGuide id={card.id} foldable open />
      {!card.available ? (
        <p className="p" data-testid="conn-unavailable">
          {t("connections.note.unavailable")}
        </p>
      ) : null}
      {fields.length > 0 ? (
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
                  disabled={testing}
                  value={values[field] ?? ""}
                  onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))}
                />
              )}
            </Field>
          ))}
        </div>
      ) : null}
      {testing ? (
        <p className="p" role="status" data-testid="add-testing">
          {t("connections.add.testing", { name: card.name })}
        </p>
      ) : null}
      {error ? (
        <div className="conn-failure">
          <p className="conn-error" role="alert" data-testid="add-error">
            {error}
          </p>
          <GuideFail id={card.id} />
        </div>
      ) : null}
      <div className="conn-actions conn-end">
        <Button onClick={onBack} disabled={testing}>
          {t("connections.add.back")}
        </Button>
        <Button type="submit" variant="primary" disabled={!filled || testing || !card.available}>
          {t("connections.add.testAndAdd")}
        </Button>
      </div>
    </form>
  );
}
