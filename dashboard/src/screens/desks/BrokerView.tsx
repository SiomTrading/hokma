import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api";
import { Button, EmptyState, ErrorState, Field, Icon, Input, Loading, Modal, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { accountsApi } from "./accountsApi";
import type { Account, Broker } from "./accountsApi";
import { AssignAccount } from "./AssignAccount";
import { formatBalance, formatCapital } from "./format";
import { NewAccountModal } from "./NewAccountModal";
import { refreshDesks, useDesks } from "./store";

interface BrokerWithAccounts extends Broker {
  accounts: Account[];
}

type Dialog = { kind: "account" } | { kind: "reset"; account: Account } | { kind: "assign"; account: Account } | { kind: "unassign"; account: Account };

/** Confirmation to reset an account: it says the capital it goes back to. */
function ResetModal({ account, onClose, onDone }: { account: Account; onClose: () => void; onDone: () => void }) {
  const { t, locale } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await accountsApi.reset(account.id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    onDone();
    onClose();
  };
  return (
    <Modal label={t("desks.accounts.reset.title")} role="alertdialog" onClose={onClose} style={{ width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{t("desks.accounts.reset.title")}</h2>
      <p className="p">{t("desks.accounts.reset.body", { name: account.name, capital: formatCapital(account.initial_capital, locale), currency: account.currency })}</p>
      {error ? <span className="err" role="alert">{error}</span> : null}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("desks.accounts.cancel")}</Button>
        <Button variant="danger" disabled={busy} onClick={() => void confirm()}>{t("desks.accounts.reset.confirm")}</Button>
      </div>
    </Modal>
  );
}

/** Confirmation to take an account off its desk. The account and its money stay. */
function UnassignModal({ account, deskName, deskId, onClose, onDone }: { account: Account; deskName: string; deskId: number; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await accountsApi.unassign(deskId);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    onDone();
    onClose();
  };
  return (
    <Modal label={t("desks.accounts.unassign.title")} role="alertdialog" onClose={onClose} style={{ width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{t("desks.accounts.unassign.title")}</h2>
      <p className="p">{t("desks.accounts.unassign.body", { name: account.name, desk: deskName })}</p>
      {error ? <span className="err" role="alert">{error}</span> : null}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("desks.accounts.cancel")}</Button>
        <Button variant="danger" disabled={busy} onClick={() => void confirm()}>{t("desks.accounts.unassign.confirm")}</Button>
      </div>
    </Modal>
  );
}

/** Commission and slippage of a broker,read-only until "Edit costs" is pressed. */
function Costs({ broker, onSaved }: { broker: Broker; onSaved: () => void }) {
  const { t, locale } = useI18n();
  const [editing, setEditing] = useState(false);
  const [fee, setFee] = useState("");
  const [slip, setSlip] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const values = [Number(fee), Number(slip)];
  const valid = [fee, slip].every((v) => v.trim() !== "") && values.every((n) => Number.isFinite(n) && n >= 0);

  const edit = () => {
    setFee(String(broker.commission.value));
    setSlip(String(broker.slippage_bps));
    setError(null);
    setEditing(true);
  };
  const save = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await accountsApi.updateCosts(broker.id, values[0], values[1]);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
      setBusy(false);
      return;
    }
    setBusy(false);
    setEditing(false);
    onSaved();
  };
  const bps = (n: number) => n.toLocaleString(locale, { maximumFractionDigits: 2 });
  return (
    <div className="brk-costs-wrap">
      <div className="brk-costs">
        <div className="brk-cost">
          {editing ? (
            <Field label={t("desks.accounts.commission")}>{(props) => <Input {...props} inputMode="decimal" value={fee} onChange={(e) => setFee(e.target.value)} />}</Field>
          ) : (
            <><span className="lbl">{t("desks.broker.commission")}</span><b className="mono">{bps(broker.commission.value)}<small>bps</small></b></>
          )}
          <span className="brk-cost-note">{t("desks.broker.commissionNote")}</span>
        </div>
        <div className="brk-cost">
          {editing ? (
            <Field label={t("desks.accounts.slippage")} hint={t("desks.accounts.costsHint")} error={error ?? undefined}>
              {(props) => <Input {...props} inputMode="decimal" value={slip} onChange={(e) => setSlip(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} />}
            </Field>
          ) : (
            <><span className="lbl">{t("desks.broker.slippage")}</span><b className="mono">{bps(broker.slippage_bps)}<small>bps</small></b></>
          )}
          <span className="brk-cost-note">{t("desks.broker.slippageNote")}</span>
        </div>
      </div>
      <span className="desk-hdr-actions">
        {editing ? (
          <>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>{t("desks.accounts.cancel")}</Button>
            <Button variant="primary" size="sm" disabled={!valid || busy} onClick={() => void save()}>{t("desks.broker.saveCosts")}</Button>
          </>
        ) : (
          <Button size="sm" onClick={edit}>{t("desks.broker.editCosts")}</Button>
        )}
      </span>
    </div>
  );
}

/** The Broker tab of the Fund: connected brokers with their costs, the way to connect others, and the accounts. */
export function BrokerView() {
  const { t, locale } = useI18n();
  const { desks } = useDesks();
  const [brokers, setBrokers] = useState<BrokerWithAccounts[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await accountsApi.brokers();
      setBrokers(await Promise.all(list.map(async (b) => ({ ...b, accounts: await accountsApi.accounts(b.id) }))));
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.accounts.error"));
    }
  }, [t]);
  useEffect(() => void load(), [load]);

  const reload = () => {
    void load();
    void refreshDesks();
  };
  const deskName = (id: number | null) => (id === null ? null : (desks.find((d) => d.id === id)?.name ?? `#${id}`));

  if (brokers === null && error) return <ErrorState message={error} onRetry={() => void load()} />;
  if (brokers === null) return <Loading label={t("desks.accounts.loading")} />;
  const first = brokers[0];
  const freeDesks = desks.some((d) => !d.account);

  return (
    <>
      <div className="brk-grid">
        <section className="pane brk-card" aria-label={t("desks.broker.connected")}>
          <div className="brk-sec-hd"><span className="lbl">{t("desks.broker.connected")}</span><span className="today-meta">{brokers.length}</span></div>
          {brokers.length === 0 ? (
            <p className="p">{t("desks.accounts.noBroker.text")}</p>
          ) : brokers.map((b) => {
            const capital = b.accounts.reduce((sum, a) => sum + a.initial_capital, 0);
            return (
              <div key={b.id} className="brk-one">
                <div className="brk-id">
                  <span className="brk-av"><Icon name="connections" size={20} /></span>
                  <div className="brk-id-txt">
                    <h2>{b.name}<Pill tone="ok">{t("desks.broker.connectedPill")}</Pill></h2>
                    <span>{t("desks.broker.included")}</span>
                  </div>
                </div>
                <p className="p">{t("desks.broker.about")}</p>
                <Costs broker={b} onSaved={() => void load()} />
                <div className="brk-stats">
                  <div><span className="lbl">{t("desks.broker.accounts")}</span><b className="mono">{b.accounts.length}</b></div>
                  <div><span className="lbl">{t("desks.broker.capital")}</span><b className="mono">{formatCapital(capital, locale)}</b></div>
                  <div><span className="lbl">{t("desks.broker.inDesks")}</span><b className="mono">{t("desks.broker.of", { n: String(b.accounts.filter((a) => a.desk_id !== null).length), total: String(b.accounts.length) })}</b></div>
                </div>
              </div>
            );
          })}
        </section>
        <section className="pane brk-connect" aria-labelledby="brk-connect-title">
          <span className="lbl">{t("desks.broker.connect")}</span>
          <h3 id="brk-connect-title">{t("desks.broker.external")}</h3>
          <p className="p">{t("desks.broker.externalText")}</p>
        </section>
      </div>

      <section className="pane brk-accts" aria-label={t("desks.broker.accountsTitle")}>
        <div className="brk-hd">
          <span className="lbl">{t("desks.broker.accountsTitle")}</span>
          <Button variant="primary" size="sm" disabled={!first} onClick={() => setDialog({ kind: "account" })}>{t("desks.accounts.newAccount")}</Button>
        </div>
        {brokers.every((b) => b.accounts.length === 0) ? (
          <EmptyState icon="desks" title={t("desks.accounts.empty.title")} description={t("desks.accounts.empty.text")}
            action={<Button variant="primary" disabled={!first} onClick={() => setDialog({ kind: "account" })}>{t("desks.accounts.empty.button")}</Button>} />
        ) : (
          <div className="brk-tbl-wrap">
            <table className="today-tbl brk-tbl">
              <thead>
                <tr>
                  <th scope="col">{t("desks.accounts.name")}</th>
                  <th scope="col">{t("desks.accounts.broker")}</th>
                  <th scope="col" className="r">{t("desks.accounts.initial")}</th>
                  <th scope="col" className="r">{t("desks.accounts.balance")}</th>
                  <th scope="col">{t("desks.accounts.desk")}</th>
                  <th scope="col"><span className="sr-only">{t("desks.accounts.actions")}</span></th>
                </tr>
              </thead>
              <tbody>
                {brokers.flatMap((b) => b.accounts).map((a) => (
                  <tr key={a.id}>
                    <th scope="row">{a.name}</th>
                    <td>{a.broker}</td>
                    <td className="r mono">{formatCapital(a.initial_capital, locale)}</td>
                    <td className="r mono">{formatBalance(a.equity, locale, t("desks.accounts.unavailable"), a.currency)}</td>
                    <td>{deskName(a.desk_id) ?? t("desks.accounts.free")}</td>
                    <td className="desk-accounts-actions">
                      <Button variant="ghost" size="sm" onClick={() => setDialog({ kind: "reset", account: a })}>{t("desks.accounts.reset")}</Button>
                      {a.desk_id === null && freeDesks ? <Button variant="ghost" size="sm" onClick={() => setDialog({ kind: "assign", account: a })}>{t("desks.accounts.assign")}</Button> : null}
                      {a.desk_id !== null ? <Button variant="ghost" size="sm" onClick={() => setDialog({ kind: "unassign", account: a })}>{t("desks.accounts.unassign")}</Button> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="brk-ft">{t("desks.broker.footnote")}</p>
      </section>
      {dialog?.kind === "account" ? <NewAccountModal brokers={brokers} onClose={() => setDialog(null)} onDone={reload} /> : null}
      {dialog?.kind === "reset" ? <ResetModal account={dialog.account} onClose={() => setDialog(null)} onDone={reload} /> : null}
      {dialog?.kind === "unassign" && dialog.account.desk_id !== null ? (
        <UnassignModal account={dialog.account} deskId={dialog.account.desk_id} deskName={deskName(dialog.account.desk_id) ?? ""} onClose={() => setDialog(null)} onDone={reload} />
      ) : null}
      {dialog?.kind === "assign" ? <AssignAccount account={dialog.account} onClose={() => setDialog(null)} onDone={reload} /> : null}
    </>
  );
}
