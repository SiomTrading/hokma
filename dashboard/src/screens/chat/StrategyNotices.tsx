import { useCallback, useEffect, useState } from "react";
import { Button, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { strategyCodeApi, type CodeRow } from "@/api/strategyCode";

const POLL_MS = 15_000;

type Answer = "approved" | "rejected";

/** The notice of a strategy that the code reviewer approved: where it was created, what it does, the code to read, and the two answers of the owner. It is drawn from what the base holds,
 *  so it is there again every time the chat opens while a version waits (SPEC-031 A2, 7). */
export function StrategyNotices() {
  const { t } = useI18n();
  const [rows, setRows] = useState<CodeRow[]>([]);
  const [answered, setAnswered] = useState<Array<{ row: CodeRow; answer: Answer }>>([]);
  const [open, setOpen] = useState<{ id: number; code: string } | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(() => {
    strategyCodeApi.generated().then((list) => setRows(Array.isArray(list?.rows) ? list.rows : [])).catch(() => undefined);
  }, []);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const answer = (row: CodeRow, how: Answer) => {
    setFailed(false);
    const call = how === "approved" ? strategyCodeApi.approve(row.rule_version_id) : strategyCodeApi.reject(row.rule_version_id, note.trim());
    call
      .then(() => {
        setAnswered((list) => [...list, { row, answer: how }]);
        setRows((list) => list.filter((r) => r.rule_version_id !== row.rule_version_id));
        setAsking(null);
        setRejecting(null);
        setNote("");
        refresh();
      })
      .catch(() => setFailed(true));
  };

  const toggleCode = (row: CodeRow) => {
    if (open?.id === row.rule_version_id) return setOpen(null);
    strategyCodeApi.read(row.rule_version_id).then((detail) => setOpen({ id: row.rule_version_id, code: detail.code })).catch(() => setFailed(true));
  };

  const where = (row: CodeRow) => (row.origin.kind === "hokma" ? t("strategy.notice.hokma") : row.origin.desk_name ?? t("strategy.notice.desk"));

  if (rows.length === 0 && answered.length === 0) return null;
  return (
    <div className="strategy-notices" aria-live="polite">
      {answered.map(({ row, answer: how }) => (
        <div key={`done-${row.rule_version_id}`} className="chat-notice">
          <Pill tone={how === "approved" ? "ok" : "warn"}>
            {row.title}: {how === "approved" ? t("strategy.notice.approved") : t("strategy.notice.rejected")}
          </Pill>
        </div>
      ))}
      {rows.map((row) => (
        <section key={row.rule_version_id} className="strategy-notice" data-testid="strategy-notice">
          <h4>{t("strategy.notice.title", { name: row.title })} <span className="strategy-notice-origin">({t("strategy.notice.origin", { where: where(row) })})</span></h4>
          {row.summary ? <p>{row.summary}</p> : null}
          <p>{t("strategy.notice.reviewed", { reviewer: row.reviewer ?? "", count: String(row.tests) })}</p>
          <p>{t("strategy.notice.ready")}</p>
          <div className="strategy-notice-actions">
            <Button size="sm" variant="ghost" onClick={() => toggleCode(row)}>
              {open?.id === row.rule_version_id ? t("strategy.notice.hideCode") : t("strategy.notice.viewCode")}
            </Button>
            {asking === row.rule_version_id ? (
              <>
                <span>{t("strategy.notice.confirmAsk")}</span>
                <Button size="sm" variant="primary" onClick={() => answer(row, "approved")}>{t("strategy.notice.confirmYes")}</Button>
                <Button size="sm" variant="ghost" onClick={() => setAsking(null)}>{t("strategy.notice.cancel")}</Button>
              </>
            ) : rejecting === row.rule_version_id ? (
              <>
                <input className="strategy-notice-note" aria-label={t("strategy.notice.note")} placeholder={t("strategy.notice.note")} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
                <Button size="sm" variant="primary" onClick={() => answer(row, "rejected")}>{t("strategy.notice.rejectSend")}</Button>
                <Button size="sm" variant="ghost" onClick={() => setRejecting(null)}>{t("strategy.notice.cancel")}</Button>
              </>
            ) : (
              <>
                <Button size="sm" variant="primary" onClick={() => setAsking(row.rule_version_id)}>{t("strategy.notice.approve")}</Button>
                <Button size="sm" variant="ghost" onClick={() => setRejecting(row.rule_version_id)}>{t("strategy.notice.reject")}</Button>
              </>
            )}
          </div>
          {open?.id === row.rule_version_id ? <pre className="strategy-notice-code" tabIndex={0}>{open.code}</pre> : null}
        </section>
      ))}
      {failed ? <div className="chat-notice" role="alert"><Pill tone="bad">{t("strategy.notice.error")}</Pill></div> : null}
    </div>
  );
}
