import { useState } from "react";
import { ApiError } from "@/api";
import { Button, Field, Input, Modal, Select } from "@/design";
import { useI18n } from "@/i18n";
import { accountsApi } from "./accountsApi";
import type { Broker } from "./accountsApi";
import { DESK_LIMITS } from "./types";

interface Props {
  brokers: Broker[];
  onClose: () => void;
  /** Called after the account exists, so the list reloads. */
  onDone: () => void;
}

const actions = { display: "flex", gap: 8, justifyContent: "flex-end" } as const;
const box = { width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 } as const;

/** New account: the broker it lives in, a name and its starting capital. */
export function NewAccountModal({ brokers, onClose, onDone }: Props) {
  const { t } = useI18n();
  const [broker, setBroker] = useState(String(brokers[0]?.id ?? ""));
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const amount = capital.trim() === "" ? Number.NaN : Number(capital);
  const valid =
    name.trim().length > 0 && Number.isInteger(amount) && amount >= DESK_LIMITS.capitalMin && amount <= DESK_LIMITS.capitalMax;

  const save = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await accountsApi.createAccount(Number(broker), name.trim(), amount);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    onDone();
    onClose();
  };

  return (
    <Modal label={t("desks.accounts.newAccount.title")} onClose={onClose} style={box}>
      <h2 className="h1">{t("desks.accounts.newAccount.title")}</h2>
      <Field label={t("desks.accounts.broker")}>
        {(props) => (
          <Select {...props} value={broker} onChange={(e) => setBroker(e.target.value)}>
            {brokers.map((item) => (
              <option key={item.id} value={String(item.id)}>{item.name}</option>
            ))}
          </Select>
        )}
      </Field>
      <Field label={t("desks.accounts.name")}>
        {(props) => <Input {...props} value={name} maxLength={60} autoComplete="off" onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label={t("desks.accounts.capital")} hint={t("desks.accounts.capitalHint")} error={error ?? undefined}>
        {(props) => (
          <Input {...props} inputMode="numeric" value={capital} onChange={(e) => setCapital(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} />
        )}
      </Field>
      <div style={actions}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("desks.accounts.cancel")}</Button>
        <Button variant="primary" disabled={!valid || busy} onClick={() => void save()}>{t("desks.accounts.newAccount.save")}</Button>
      </div>
    </Modal>
  );
}
