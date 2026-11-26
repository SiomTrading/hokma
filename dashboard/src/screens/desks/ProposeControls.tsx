import { useState } from "react";
import { Button, Select } from "@/design";
import { useI18n } from "@/i18n";
import { AssignAccount } from "./AssignAccount";
import { failText } from "./configApi";
import { detailApi, PRINCIPAL } from "./optionDetailApi";
import type { DetailRequest, ProposeResult } from "./optionDetailApi";
import { refreshDesks } from "./store";
import type { DeskSummary } from "./types";

interface Props {
  desk: Pick<DeskSummary, "id" | "name" | "accounts">;
  /** The strategy to propose; null while there is none to propose. */
  request: DetailRequest | null;
  /** The strategy_key the order is bound to (SPEC-027): the principal one unless the strategy was saved under its own key. */
  strategyKey: string | null;
  /** The reason the button is off, shown as its title (a card with the lock of premium selling, one that does not fit the budget). */
  disabledReason?: string | null;
}

/** The answers that left a proposal recorded: asking again would make a second one. */
const RECORDED = new Set(["awaiting_owner", "sent", "filled"]);

/**
 * "Proponer" of the Option detail and Strategies panels. The proposal goes to the door of execution of the desk and never sends by itself: the answer says
 * whether it waits for the owner, only analyzes, or was refused by a rule of the owner or the lock of premium selling, each with its reason. A desk without
 * account opens "Add account"; a strategy without account asks which account of the desk it goes out by.
 */
export function ProposeControls({ desk, request, strategyKey, disabledReason }: Props) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ProposeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [account, setAccount] = useState("");
  /** The strategy a proposal was recorded for (it waits for the owner, or went out): the button stays off for it until the strategy changes, so a second click is not a duplicate. */
  const [recorded, setRecorded] = useState<string | null>(null);
  const signature = request ? JSON.stringify([request, strategyKey ?? PRINCIPAL]) : null;

  const run = async () => {
    if (!request || busy || (signature !== null && signature === recorded)) return;
    setBusy(true);
    setError(null);
    try {
      const answer = await detailApi.propose(desk.id, { ...request, strategy_key: strategyKey ?? PRINCIPAL });
      setResult(answer);
      setRecorded(RECORDED.has(answer.status) ? signature : null);
    } catch (cause) {
      setResult(null);
      setError(failText(cause, t("desks.detail.proposeError")));
    } finally {
      setBusy(false);
    }
  };

  const bind = async () => {
    const key = result?.strategy_key ?? strategyKey ?? PRINCIPAL;
    const chosen = Number(account || result?.accounts?.[0]?.id);
    if (!chosen || busy) return;
    setBusy(true);
    setError(null);
    try {
      await detailApi.bind(desk.id, key, chosen);
      await refreshDesks();
    } catch (cause) {
      setError(failText(cause, t("desks.detail.bindError")));
      setBusy(false);
      return;
    }
    setBusy(false);
    setAccount("");
    void run();
  };

  const done = signature !== null && signature === recorded;
  const needs = result?.status === "needs_account";
  const tone = result?.status === "refused" || result?.status === "no_data" || result?.status === "rejected_by_broker" ? "bad" : "ok";
  return (
    <div className="opd-propose">
      <Button size="sm" variant="primary" disabled={!request || busy || done || Boolean(disabledReason)} title={disabledReason ?? undefined} onClick={() => void run()}>
        {t("desks.detail.propose")}
      </Button>
      {error ? (
        <p className="strat-error" role="alert">
          {error}
        </p>
      ) : null}
      {result && !needs ? (
        <p className={`opd-answer ${tone}`} role="status">
          <strong>{t(`desks.detail.status.${result.status}`)}</strong> {result.why}
        </p>
      ) : null}
      {needs ? (
        <div className="opd-account" role="group" aria-label={t("desks.detail.accountTitle")}>
          <p className="opd-answer warn" role="status">
            {result.why}
          </p>
          {result.code === "no_account" ? (
            <Button size="sm" onClick={() => setAdding(true)}>
              {t("desks.detail.addAccount")}
            </Button>
          ) : (
            <>
              <Select aria-label={t("desks.detail.pickAccount")} value={account || String(result.accounts?.[0]?.id ?? "")} onChange={(e) => setAccount(e.target.value)}>
                {(result.accounts ?? []).map((a) => (
                  <option key={a.id} value={String(a.id)}>
                    {a.name}
                    {a.mask ? ` ${a.mask}` : ""} ({a.broker})
                  </option>
                ))}
              </Select>
              <Button size="sm" disabled={busy} onClick={() => void bind()}>
                {t("desks.detail.useAccount")}
              </Button>
            </>
          )}
        </div>
      ) : null}
      {adding ? <AssignAccount desk={desk} onClose={() => setAdding(false)} onDone={() => void run()} /> : null}
    </div>
  );
}
