import { useEffect, useState } from "react";
import { ApiError } from "@/api";
import { Button, Field, Loading, Modal, Select } from "@/design";
import { useI18n } from "@/i18n";
import { accountsApi } from "./accountsApi";
import type { Account } from "./accountsApi";
import { refreshDesks, useDesks } from "./store";
import type { DeskSummary } from "./types";

type Props = {
  onClose: () => void;
  /** Called after the assignment, so the accounts list reloads. */
  onDone?: () => void;
} & ({ desk: Pick<DeskSummary, "id" | "name">; account?: never } | { account: Pick<Account, "id" | "name">; desk?: never });

/**
 * Links an account to a desk. From a desk it offers the accounts nobody uses; from an account it offers the desks that
 * have none. When there is nothing to pick it says so and does not leave the choice empty.
 */
export function AssignAccount({ desk, account, onClose, onDone }: Props) {
  const { t } = useI18n();
  const { desks } = useDesks();
  const [free, setFree] = useState<Account[] | null>(desk ? null : []);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!desk) return;
    void (async () => {
      try {
        const brokers = await accountsApi.brokers();
        const all = (await Promise.all(brokers.map((b) => accountsApi.accounts(b.id)))).flat();
        setFree(all.filter((a) => a.desk_id === null));
      } catch (cause) {
        setFree([]);
        setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      }
    })();
  }, [desk, t]);

  const options = desk ? (free ?? []).map((a) => ({ id: a.id, label: a.name, group: a.broker })) : desks.filter((d) => !d.account).map((d) => ({ id: d.id, label: d.name, group: "" }));
  const groups = [...new Set(options.map((o) => o.group))];
  const chosen = pick || String(options[0]?.id ?? "");

  const save = async () => {
    if (!chosen || busy) return;
    setBusy(true);
    setError(null);
    try {
      await (desk ? accountsApi.assign(desk.id, Number(chosen)) : accountsApi.assign(Number(chosen), account.id));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    void refreshDesks();
    onDone?.();
    onClose();
  };

  const title = t("desks.accounts.assign.title", { name: (desk ?? account).name });
  return (
    <Modal label={title} onClose={onClose} style={{ width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{title}</h2>
      {free === null ? (
        <Loading label={t("desks.accounts.loading")} />
      ) : options.length === 0 ? (
        <p className="p" role={error ? "alert" : undefined}>{error ?? t(desk ? "desks.accounts.assign.noAccounts" : "desks.accounts.assign.noDesks")}</p>
      ) : (
        <Field label={t(desk ? "desks.accounts.assign.pickAccount" : "desks.accounts.assign.pickDesk")} error={error ?? undefined}>
          {(props) => (
            <Select {...props} value={chosen} onChange={(e) => setPick(e.target.value)}>
              {groups.map((group) => {
                const items = options.filter((o) => o.group === group).map((o) => (
                  <option key={o.id} value={String(o.id)}>{o.label}</option>
                ));
                return group ? <optgroup key={group} label={group}>{items}</optgroup> : items;
              })}
            </Select>
          )}
        </Field>
      )}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("desks.accounts.cancel")}</Button>
        <Button variant="primary" disabled={!chosen || busy || free === null} onClick={() => void save()}>{t("desks.accounts.assign.save")}</Button>
      </div>
    </Modal>
  );
}
