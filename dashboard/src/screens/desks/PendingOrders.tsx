import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import type { Account } from "./accountsApi";
import { minutesLeft, pendingApi, summarizeOrder } from "./pendingApi";
import type { PendingOrder } from "./pendingApi";

/** Orders waiting for the owner, polled every 15 s while the Broker tab is open. */
const REFRESH_MS = 15000;

interface Row extends PendingOrder {
  deskId: number;
}

type Outcome = { id: number; tone: "ok" | "warn"; text: string };

/**
 * "Orders that wait for your approval" (SPEC-015 18.3). `desks` are the desks that have an account of a broker with orders.
 * Hidden while nothing waits. Approving previews again on the server and sends only if the rules still pass.
 */
export function PendingOrders({ desks }: { desks: Array<{ id: number; name: string; account: Account | null }> }) {
  const { t } = useI18n();
  const [rows, setRows] = useState<Row[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState<number | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const key = desks.map((d) => d.id).join(",");
  const deskIds = useRef<number[]>([]);
  deskIds.current = desks.map((d) => d.id);

  const load = useCallback(async () => {
    const ids = deskIds.current;
    const lists = await Promise.all(ids.map(async (id) => {
      try {
        return (await pendingApi.list(id)).map((p): Row => ({ ...p, deskId: id }));
      } catch {
        return [];
      }
    }));
    setRows(lists.flat());
    setNow(Date.now());
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load, key]);

  const deskOf = (id: number) => desks.find((d) => d.id === id);

  const decide = async (row: Row, approve: boolean) => {
    if (busy !== null) return;
    setBusy(row.decision_id);
    try {
      if (approve) {
        const result = await pendingApi.approve(row.deskId, row.decision_id);
        setOutcome(result.status === "not_sent"
          ? { id: row.decision_id, tone: "warn", text: t("desks.pending.notSent", { why: result.why ?? "" }) }
          : { id: row.decision_id, tone: "ok", text: t("desks.pending.sent") });
      } else {
        await pendingApi.reject(row.deskId, row.decision_id);
        setOutcome({ id: row.decision_id, tone: "ok", text: t("desks.pending.rejected") });
      }
    } catch (cause) {
      const expired = cause instanceof ApiError && cause.code === "decision_expired";
      const message = cause instanceof ApiError ? cause.message : t("desks.accounts.error");
      setOutcome({ id: row.decision_id, tone: "warn", text: expired ? t("desks.pending.expired", { message }) : message });
    }
    setBusy(null);
    await load();
  };

  if (rows.length === 0 && !outcome) return null;
  return (
    <section className="pane brk-pending" aria-labelledby="brk-pending-title">
      <div className="brk-hd">
        <span className="lbl" id="brk-pending-title">{t("desks.pending.title")}</span>
        <span className="today-meta">{rows.length}</span>
      </div>
      {outcome ? (
        <div className={`brk-pending-out ${outcome.tone}`} role="status">
          <span>{outcome.text}</span>
          <Button variant="ghost" size="sm" onClick={() => setOutcome(null)}>{t("desks.pending.dismiss")}</Button>
        </div>
      ) : null}
      <ul className="brk-pending-list">
        {rows.map((row) => {
          const desk = deskOf(row.deskId);
          const summary = summarizeOrder(row.order);
          const left = minutesLeft(row.expires_at, now);
          const who = desk?.account?.mask ? `${desk.name} ${desk.account.mask}` : (desk?.name ?? `#${row.deskId}`);
          return (
            <li key={`${row.deskId}-${row.decision_id}`} className="brk-pending-one">
              <div className="brk-pending-hd">
                <b>{who}</b>
                <span className="mono brk-pending-order">{summary}</span>
                <span className="today-meta">{left > 0 ? t("desks.pending.left", { n: String(left) }) : t("desks.pending.over")}</span>
              </div>
              {row.reason ? <p className="p"><span className="lbl">{t("desks.pending.reason")}</span> {row.reason}</p> : null}
              {row.why ? <p className="p"><span className="lbl">{t("desks.pending.why")}</span> {row.why}</p> : null}
              <div className="desk-hdr-actions">
                <Button variant="ghost" size="sm" disabled={busy !== null} aria-label={t("desks.pending.rejectAria", { desk: who, order: summary })} onClick={() => void decide(row, false)}>{t("desks.pending.reject")}</Button>
                <Button variant="primary" size="sm" disabled={busy !== null} aria-label={t("desks.pending.approveAria", { desk: who, order: summary })} onClick={() => void decide(row, true)}>{t("desks.pending.approve")}</Button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
