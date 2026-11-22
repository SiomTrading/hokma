import { useState } from "react";
import { Button, IconButton, Loading, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { connectionErrorText, schwabApi } from "./api";
import type { DiagnosticAnswer } from "./types";

const SECONDS = 75;

/** The Schwab data check (SPEC-016 T1). One button: Hokma listens to Schwab for about a minute and saves what it saw in a file. It reads only:
 *  it sends no order and the file has no token, no account number and no session id. */
export function SchwabDiagnosticDialog({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const [phase, setPhase] = useState<"idle" | "running" | "done" | "error">("idle");
  const [answer, setAnswer] = useState<DiagnosticAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = () => {
    setPhase("running");
    setError(null);
    schwabApi.diagnostic(SECONDS).then(
      (data) => {
        setAnswer(data);
        setPhase("done");
      },
      (cause: unknown) => {
        setError(connectionErrorText(cause));
        setPhase("error");
      },
    );
  };

  return (
    <Modal label={t("connections.schwab.diagnostic")} onClose={onClose} className="conn-dialog">
      <div className="conn-dialog-body" data-testid="schwab-diagnostic">
        <header className="conn-dialog-head">
          <h2 className="h1">{t("connections.schwab.diagnostic")}</h2>
          <IconButton icon="close" label={t("connections.add.close")} onClick={onClose} />
        </header>
        <p className="p">{t("connections.schwab.diagnostic.what", { seconds: SECONDS })}</p>
        <p className="p conn-note">{t("connections.schwab.diagnostic.safe")}</p>
        {phase === "running" ? <Loading label={t("connections.schwab.diagnostic.running")} /> : null}
        {phase === "error" ? (
          <p className="conn-error" role="alert" data-testid="schwab-diagnostic-error">
            {error}
          </p>
        ) : null}
        {phase === "done" && answer ? <Result answer={answer} /> : null}
        <Button variant="primary" disabled={phase === "running"} onClick={run} data-testid="schwab-diagnostic-run">
          {phase === "done" || phase === "error" ? t("connections.schwab.diagnostic.again") : t("connections.schwab.diagnostic.run")}
        </Button>
      </div>
    </Modal>
  );
}

function Result({ answer }: { answer: DiagnosticAnswer }) {
  const { t } = useI18n();
  const beat = answer.summary.heartbeat_interval_ms;
  return (
    <div role="status" data-testid="schwab-diagnostic-result">
      <p className="p">{t("connections.schwab.diagnostic.saved")}</p>
      <p className="mono" data-testid="schwab-diagnostic-file">
        {answer.file}
      </p>
      <dl className="conn-balances">
        <div className="conn-balance">
          <dt className="p">{t("connections.schwab.diagnostic.messages")}</dt>
          <dd className="mono">{answer.messages}</dd>
        </div>
        <div className="conn-balance">
          <dt className="p">{t("connections.schwab.diagnostic.heartbeat")}</dt>
          <dd className="mono">{beat.median === null ? "-" : beat.median + " ms"}</dd>
        </div>
        <div className="conn-balance">
          <dt className="p">{t("connections.schwab.diagnostic.chart")}</dt>
          <dd className="mono">{answer.summary.chart_equity.mode}</dd>
        </div>
      </dl>
      <p className="p">{t("connections.schwab.diagnostic.send")}</p>
    </div>
  );
}
