import { useEffect, useMemo, useState } from "react";
import { ApiError } from "@/api";
import { Button, Field, Input, Loading, Modal, Option, Select } from "@/design";
import { useI18n } from "@/i18n";
import { accountsApi, isRealBroker } from "./accountsApi";
import type { Account } from "./accountsApi";
import { parseWhole } from "./parse";
import { refreshDesks, useDesks } from "./store";
import { accountsOf, DESK_LIMITS } from "./types";
import type { DeskSummary } from "./types";

type Props = {
  onClose: () => void;
  /** Called after the account was added, so the accounts list reloads. */
  onDone?: () => void;
} & ({ desk: Pick<DeskSummary, "id" | "name" | "accounts">; account?: never } | { account: Pick<Account, "id" | "name" | "desk_ids">; desk?: never });

const box = { width: "min(480px, 100%)", flexDirection: "column", padding: 22, gap: 12 } as const;

/**
 * "Add account" (SPEC-027). From a desk: a new demo account (the name and the starting capital are the owner's) or an account that
 * already exists, even if other desks use it. From an account: the desk that will use it. The first account of a desk also becomes
 * the account of its main strategy: the single click on the button covers both and the dialog says so before.
 */
export function AssignAccount({ desk, account, onClose, onDone }: Props) {
  const { t } = useI18n();
  const { desks } = useDesks();
  const [all, setAll] = useState<Account[] | null>(desk ? null : []);
  const [canDemo, setCanDemo] = useState(false);
  const [mode, setMode] = useState<"demo" | "existing">("demo");
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("");
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [showGaps, setShowGaps] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!desk) return;
    let alive = true;
    void (async () => {
      try {
        const brokers = await accountsApi.brokers();
        const list = (await Promise.all(brokers.map((b) => accountsApi.accounts(b.id)))).flat();
        if (!alive) return;
        setCanDemo(brokers.some((b) => !isRealBroker(b)));
        setAll(list);
      } catch (cause) {
        if (!alive) return;
        setAll([]);
        setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      }
    })();
    return () => {
      alive = false;
    };
  }, [desk, t]);

  const deskName = (id: number) => desks.find((d) => d.id === id)?.name ?? `#${id}`;
  /** From a desk: the accounts it does not hold yet. From an account: the desks that do not hold it. */
  const options = useMemo(() => {
    if (desk) {
      return (all ?? [])
        .filter((a) => !a.desk_ids.includes(desk.id))
        .map((a) => ({ id: a.id, label: a.name, group: a.broker, others: a.desk_ids }));
    }
    return desks.filter((d) => !account.desk_ids.includes(d.id)).map((d) => ({ id: d.id, label: d.name, group: "", others: [] as number[] }));
  }, [all, desk, account, desks]);
  const groups = [...new Set(options.map((o) => o.group))];
  const chosen = pick || String(options[0]?.id ?? "");
  const capitalValue = parseWhole(capital);
  const demoGaps = { name: name.trim() === "", capital: capitalValue === null || capitalValue < DESK_LIMITS.capitalMin || capitalValue > DESK_LIMITS.capitalMax };
  const demo = desk !== undefined && canDemo && mode === "demo";
  const target = desk ?? desks.find((d) => String(d.id) === chosen);
  const first = target ? accountsOf(target).length === 0 : false;
  const ready = desk ? (demo ? true : chosen !== "") : chosen !== "";

  const save = async () => {
    if (busy || !ready) return;
    if (demo && (demoGaps.name || demoGaps.capital)) {
      setShowGaps(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (desk) {
        await (demo ? accountsApi.addDemoToDesk(desk.id, name.trim(), capitalValue as number) : accountsApi.addToDesk(desk.id, Number(chosen)));
      } else {
        await accountsApi.addToDesk(Number(chosen), account.id);
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    void refreshDesks();
    onDone?.();
    onClose();
  };

  const title = desk ? t("desks.accounts.add.title", { name: desk.name }) : t("desks.accounts.add.toDesk", { name: account.name });
  const picker = (
    <Field label={t(desk ? "desks.accounts.assign.pickAccount" : "desks.accounts.assign.pickDesk")} error={!demo ? (error ?? undefined) : undefined}>
      {(props) => (
        <Select {...props} value={chosen} onChange={(e) => setPick(e.target.value)}>
          {groups.map((group) => {
            const items = options.filter((o) => o.group === group).map((o) => (
              <option key={o.id} value={String(o.id)}>
                {o.label}{o.others.length > 0 ? ` · ${t("desks.accounts.add.alsoIn", { desks: o.others.map(deskName).join(", ") })}` : ""}
              </option>
            ));
            return group ? <optgroup key={group} label={group}>{items}</optgroup> : items;
          })}
        </Select>
      )}
    </Field>
  );

  let body;
  if (desk && all === null) body = <Loading label={t("desks.accounts.loading")} />;
  else {
    body = (
      <>
        {desk ? (
          <div role="radiogroup" aria-label={title} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {canDemo ? (
              <Option selected={mode === "demo"} onSelect={() => setMode("demo")}>
                <b>{t("desks.accounts.add.demo")}</b>
                <span className="desk-tpl-text">{t("desks.accounts.add.demoText")}</span>
              </Option>
            ) : null}
            <Option selected={mode === "existing" || !canDemo} onSelect={() => setMode("existing")}>
              <b>{t("desks.accounts.add.existing")}</b>
              <span className="desk-tpl-text">{t("desks.accounts.add.existingText")}</span>
            </Option>
          </div>
        ) : null}
        {demo ? (
          <>
            <Field label={t("desks.accounts.name")} error={showGaps && demoGaps.name ? t("desks.wizard.required") : undefined}>
              {(props) => <Input {...props} value={name} maxLength={60} autoComplete="off" autoFocus onChange={(e) => setName(e.target.value)} />}
            </Field>
            <Field label={t("desks.accounts.capital")} hint={t("desks.accounts.capitalHint")} error={showGaps && demoGaps.capital ? t("desks.accounts.capitalHint") : (error ?? undefined)}>
              {(props) => <Input {...props} className="mono" inputMode="numeric" value={capital} onChange={(e) => setCapital(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} />}
            </Field>
          </>
        ) : options.length === 0 ? (
          <p className="p" role={error ? "alert" : undefined}>{error ?? t(desk ? "desks.accounts.add.noAccounts" : "desks.accounts.add.noDesks")}</p>
        ) : (
          picker
        )}
        {first && ready ? <p className="hint">{t("desks.accounts.add.firstNote")}</p> : null}
      </>
    );
  }

  return (
    <Modal label={title} onClose={onClose} style={box}>
      <h2 className="h1">{title}</h2>
      {body}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("desks.accounts.cancel")}</Button>
        <Button variant="primary" disabled={busy || (desk ? all === null : false) || (!demo && options.length === 0)} onClick={() => void save()}>
          {t("desks.accounts.add.save")}
        </Button>
      </div>
    </Modal>
  );
}
