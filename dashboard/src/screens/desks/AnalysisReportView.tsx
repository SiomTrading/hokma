import { useState } from "react";
import { Button, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { formatPrice } from "../chart/format";
import { AssignAccount } from "./AssignAccount";
import { failText } from "./configApi";
import { DeskChat } from "./DeskChat";
import { pendingApi } from "./pendingApi";
import type { AnalysisAgent, AnalysisReport } from "./analysisApi";
import type { DeskSummary } from "./types";

interface Props {
  desk: DeskSummary;
  report: AnalysisReport;
  /** The run is still going: the steps move and the rest waits. */
  live: boolean;
  /** The report changed outside (a decision was taken, an account was added): read it again. */
  onChanged: () => void;
  onAnalyzeOther: () => void;
}

/** One analysis: steps, grade and note of the chief, reasons, proposal, what each agent did, chat about it and the owner's decision. */
export function AnalysisReportView({ desk, report, live, onChanged, onAnalyzeOther }: Props) {
  const { t } = useI18n();
  const [chat, setChat] = useState(false);
  const symbol = report.symbol ?? "-";
  return (
    <div className="analysis-report">
      <ol className="analysis-steps" aria-label={t("desks.analysis.steps")}>
        {report.steps.map((item) => (
          <li key={item.node} data-state={item.state} aria-current={item.state === "running" ? "step" : undefined}>
            <span className="analysis-step-n mono">{item.step}</span>
            <b>{t(`desks.analysis.step.${item.node}`)}</b>
            <span className="analysis-step-s">{t(`desks.analysis.state.${item.state}`)}</span>
          </li>
        ))}
      </ol>

      {live ? (
        <p className="analysis-live" role="status">
          {t("desks.analysis.live", { symbol })}
        </p>
      ) : (
        <>
          <Verdict report={report} />
          <Proposal report={report} />
          <Agents agents={report.agents} />
          <Decision desk={desk} report={report} onChanged={onChanged} onAnalyzeOther={onAnalyzeOther} />
          <div className="analysis-ask">
            <Button variant="ghost" aria-expanded={chat} onClick={() => setChat(!chat)}>
              {chat ? t("desks.analysis.chat.hide") : t("desks.analysis.chat.show")}
            </Button>
            {chat ? (
              <div className="analysis-chat">
                <DeskChat desk={desk} about={t("desks.analysis.about", { id: String(report.run_id), symbol })} />
              </div>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}

function Verdict({ report }: { report: AnalysisReport }) {
  const { t } = useI18n();
  const failed = report.status === "failed" || report.status === "interrupted";
  return (
    <section className="analysis-verdict" aria-label={t("desks.analysis.verdict")}>
      <header>
        <b className="analysis-symbol">{report.symbol ?? "-"}</b>
        {report.rating ? <Pill tone="acc">{report.rating}</Pill> : null}
        {report.grade ? <span className="mono">{report.grade}</span> : null}
        {failed ? <Pill tone="warn">{t(`desks.analysis.status.${report.status}`)}</Pill> : null}
      </header>
      {report.summary ? (
        <p className="analysis-summary">
          <span className="lbl">{t("desks.analysis.note")}</span> {report.summary}
        </p>
      ) : null}
      {report.error ? <p className="analysis-note">{t("desks.analysis.failedRun", { error: report.error })}</p> : null}
      {report.why.length > 0 ? (
        <>
          <h3>{t("desks.analysis.why")}</h3>
          <ul className="analysis-why">
            {report.why.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function Proposal({ report }: { report: AnalysisReport }) {
  const { t, locale } = useI18n();
  const proposal = report.proposal;
  const money = (value: number | string | null | undefined) => (value === null || value === undefined || value === "" ? t("desks.analysis.noData") : formatPrice(Number(value), locale));
  const text = (value: string | number | null | undefined) => (value === null || value === undefined || value === "" ? t("desks.analysis.noData") : String(value));
  return (
    <section className="analysis-proposal" aria-label={t("desks.analysis.proposal")}>
      <h3>{t("desks.analysis.proposal")}</h3>
      {proposal === null ? (
        <p className="hint">{t("desks.analysis.noProposal")}</p>
      ) : (
        <table className="dterm-table">
          <tbody>
            <tr><th scope="row">{t("desks.analysis.p.structure")}</th><td>{text(proposal.structure)}</td></tr>
            <tr><th scope="row">{t("desks.analysis.p.underlying")}</th><td>{text(proposal.underlying)}</td></tr>
            <tr><th scope="row">{t("desks.analysis.p.expiration")}</th><td>{text(proposal.expiration)}</td></tr>
            <tr><th scope="row">{t("desks.analysis.p.contracts")}</th><td>{text(proposal.contracts)}</td></tr>
            <tr><th scope="row">{t("desks.analysis.p.premium")}</th><td>{money(proposal.net_premium)}</td></tr>
            <tr><th scope="row">{t("desks.analysis.p.maxLoss")}</th><td>{money(proposal.max_loss_usd)}</td></tr>
          </tbody>
        </table>
      )}
    </section>
  );
}

function Agents({ agents }: { agents: AnalysisAgent[] }) {
  const { t } = useI18n();
  if (agents.length === 0) return null;
  return (
    <section className="analysis-agents" aria-label={t("desks.analysis.agents")}>
      <h3>{t("desks.analysis.agents")}</h3>
      {agents.map((agent) => (
        <details key={`${agent.step}-${agent.seat}`} className="analysis-agent" data-status={agent.status}>
          <summary>
            <b>{agent.name}</b>
            <span className="analysis-agent-role">{agent.role}</span>
            {agent.status === "ok" ? null : <Pill tone="warn">{agent.status === "absent" ? t("desks.analysis.agent.absent") : t("desks.analysis.agent.failed")}</Pill>}
          </summary>
          {agent.status === "ok" ? (
            <>
              {agent.summary ? <p>{agent.summary}</p> : null}
              {agent.report ? <p className="analysis-agent-report">{agent.report}</p> : null}
              {agent.why ? (
                <p>
                  <span className="lbl">{t("desks.analysis.agent.why")}</span> {agent.why}
                </p>
              ) : null}
            </>
          ) : (
            <p>{agent.why || t("desks.analysis.agent.noCause")}</p>
          )}
          <p className="analysis-agent-tools">
            <span className="lbl">{t("desks.analysis.agent.tools")}</span> {agent.tools.length > 0 ? agent.tools.join(", ") : t("desks.analysis.agent.noTools")}
          </p>
        </details>
      ))}
    </section>
  );
}

function Decision({ desk, report, onChanged, onAnalyzeOther }: { desk: DeskSummary; report: AnalysisReport; onChanged: () => void; onAnalyzeOther: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const decision = report.decision;

  const act = (kind: "approve" | "reject") => {
    if (busy || !decision || decision.decision_id === null) return;
    setBusy(true);
    setMessage(null);
    const call = kind === "approve" ? pendingApi.approve(desk.id, decision.decision_id) : pendingApi.reject(desk.id, decision.decision_id);
    call
      .then((out) => {
        const result = out as { status: string; why?: string };
        setMessage(
          kind === "reject" ? t("desks.analysis.decision.rejected") : result.status === "not_sent" ? t("desks.analysis.decision.notSent", { why: result.why ?? "" }) : t("desks.analysis.decision.sent"),
        );
        onChanged();
      })
      .catch((cause) => setMessage(failText(cause, t("desks.analysis.decision.failed"))))
      .finally(() => setBusy(false));
  };

  return (
    <section className="analysis-decision" aria-label={t("desks.analysis.decision")}>
      <h3>{t("desks.analysis.decision")}</h3>
      {decision === null ? (
        <>
          <p>{whyNone(report, t)}</p>
          <Button variant="ghost" onClick={onAnalyzeOther}>
            {t("desks.analysis.other")}
          </Button>
        </>
      ) : decision.needs_account ? (
        <>
          <p>{t("desks.analysis.needsAccount")}</p>
          <Button variant="primary" onClick={() => setAdding(true)}>
            {t("desks.analysis.addAccount")}
          </Button>
        </>
      ) : decision.pending && decision.decision_id !== null ? (
        <div className="analysis-decision-bar">
          <Button variant="primary" disabled={busy} onClick={() => act("approve")}>
            {t("desks.analysis.take")}
          </Button>
          <Button variant="ghost" disabled={busy} onClick={() => act("reject")}>
            {t("desks.analysis.skip")}
          </Button>
        </div>
      ) : (
        <p>{t("desks.analysis.decision.status", { status: decision.status })}</p>
      )}
      {message ? (
        <p className="analysis-note" role="status">
          {message}
        </p>
      ) : null}
      {adding ? (
        <AssignAccount
          desk={desk}
          onClose={() => setAdding(false)}
          onDone={() => {
            setAdding(false);
            onChanged();
          }}
        />
      ) : null}
    </section>
  );
}

type Translate = ReturnType<typeof useI18n>["t"];

/** Why a run has no proposal to decide: where it stopped and what it said, or that the desk only analyzes (level 1). */
function whyNone(report: AnalysisReport, t: Translate): string {
  const parts: string[] = [];
  if (report.stopped_at) parts.push(t("desks.analysis.stoppedAt", { step: t(`desks.analysis.step.${report.stopped_at}`) }));
  const said = report.detail ?? report.reason;
  if (said) parts.push(said);
  if (report.level === 1) parts.push(t("desks.analysis.level1"));
  return parts.length > 0 ? parts.join(". ") : t("desks.analysis.noProposal");
}
