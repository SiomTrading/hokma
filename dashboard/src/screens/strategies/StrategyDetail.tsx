import { useCallback, useEffect, useState } from "react";
import { Button, ErrorState, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { strategyBankApi } from "@/api/strategyBank";
import type { BankDetail } from "@/api/strategyBank";
import { strategyCodeApi } from "@/api/strategyCode";
import type { CodeRow } from "@/api/strategyCode";
import { locators, ruleLines } from "./rules";

/** What the owner sends to the chat to optimize a strategy of the bank: the chat launches backtest_optimize with it. */
export function optimizeText(detail: Pick<BankDetail, "bank_id" | "title"> & { version: { version: number } }): string {
  return `Run backtest_optimize for the strategy of the bank with bank_id ${detail.bank_id} (${detail.title}, version ${detail.version.version}) and tell me the verdict when it ends.`;
}

export function StrategyDetail({ bankId, onBack }: { bankId: number; onBack: () => void }) {
  const { t } = useI18n();
  const { openScreen } = useShell();
  const [version, setVersion] = useState<number | undefined>(undefined);
  const [detail, setDetail] = useState<BankDetail | null>(null);
  const [codes, setCodes] = useState<CodeRow[]>([]);
  const [code, setCode] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<"approved" | "error" | "copied" | null>(null);

  const load = useCallback(() => {
    setFailed(false);
    Promise.all([strategyBankApi.read(bankId, version), strategyBankApi.code(bankId).catch(() => ({ rows: [] as CodeRow[] }))])
      .then(([d, c]) => {
        setDetail(d);
        setCodes(Array.isArray(c?.rows) ? c.rows : []);
      })
      .catch(() => setFailed(true));
  }, [bankId, version]);
  useEffect(load, [load]);

  const row = detail?.version.strategy_id ? codes.find((c) => c.registry_id === detail.version.strategy_id) : undefined;
  const rowId = row?.rule_version_id;
  useEffect(() => {
    setCode(null);
    if (rowId === undefined) return;
    let live = true;
    strategyCodeApi
      .read(rowId)
      .then((d) => live && setCode(d.code))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [rowId]);

  if (failed) return <><Button size="sm" variant="ghost" onClick={onBack}>{t("strategies.back")}</Button><ErrorState message={t("strategies.error")} /></>;
  if (!detail) return <Loading />;

  const lines = ruleLines(detail.version.extraction);
  const origin = detail.version.origin.kind === "hokma" ? t("strategy.notice.hokma") : detail.version.origin.desk_name ?? t("strategy.notice.desk");
  const waiting = row?.stage === "generated";

  const approve = () => {
    if (!row) return;
    strategyCodeApi
      .approve(row.rule_version_id)
      .then(() => {
        setAnswer("approved");
        setAsking(false);
        load();
      })
      .catch(() => setAnswer("error"));
  };
  const optimize = () => {
    const text = optimizeText(detail);
    const done = () => {
      setAnswer("copied");
      openScreen("chat");
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
    else done();
  };

  return (
    <>
      <div className="str-head">
        <Button size="sm" variant="ghost" onClick={onBack}>{t("strategies.back")}</Button>
        <h1 className="disp">{detail.title}</h1>
        <Pill>{t(`strategies.status.${detail.status}`)}</Pill>
      </div>
      <div className="str-meta">
        <span>{t("strategies.col.origin")}: {origin}</span>
        <span>{t("strategies.col.version")}: v{detail.version.version}</span>
        <span>{t("strategies.col.source")}: {[detail.source.kind, detail.source.reference].filter(Boolean).join(": ")}</span>
      </div>

      <div className="str-actions">
        <Button size="sm" onClick={optimize}>{t("strategies.optimize")}</Button>
        {asking ? (
          <>
            <span>{t("strategy.notice.confirmAsk")}</span>
            <Button size="sm" variant="primary" onClick={approve}>{t("strategy.notice.confirmYes")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setAsking(false)}>{t("strategy.notice.cancel")}</Button>
          </>
        ) : (
          <Button size="sm" variant="primary" disabled={!waiting} title={waiting ? undefined : t("strategies.approve.unavailable")} onClick={() => setAsking(true)}>{t("strategy.notice.approve")}</Button>
        )}
        {answer === "approved" ? <Pill tone="ok">{t("strategy.notice.approved")}</Pill> : null}
        {answer === "error" ? <Pill tone="bad">{t("strategy.notice.error")}</Pill> : null}
      </div>
      {answer === "copied" ? <p className="str-note" role="status">{t("strategies.optimize.copied")}</p> : null}

      <section className="str-section" data-testid="strategy-rules">
        <h2>{t("strategies.rules")}</h2>
        {lines.length === 0 ? <p className="str-note">{t("strategies.rules.empty")}</p> : null}
        {lines.map((line, i) => (
          <div key={`${line.path}-${i}`} className="str-rule">
            <span className="str-rule-path">{line.path}</span>
            <span>{line.value}</span>
            {line.cite.length > 0 ? <span className="str-rule-cite">{t("strategies.cite")}: {locators(line.cite, detail.source.cited).join(", ")}</span> : line.origin === "proposed" ? <span className="str-rule-cite">{t("strategies.proposed")}</span> : null}
          </div>
        ))}
      </section>

      <section className="str-section">
        <h2>{t("strategies.versions")}</h2>
        <div className="str-versions">
          {detail.versions.map((v) => (
            <Button key={v.version} size="sm" variant={v.version === detail.version.version ? "primary" : "ghost"} aria-pressed={v.version === detail.version.version} onClick={() => setVersion(v.version)}>
              v{v.version}
            </Button>
          ))}
        </div>
      </section>

      <section className="str-section">
        <h2>{t("strategies.code")}</h2>
        {code !== null ? <pre className="str-code" tabIndex={0} data-testid="strategy-code">{code}</pre> : <p className="str-note">{t("strategies.code.none")}</p>}
      </section>
    </>
  );
}
