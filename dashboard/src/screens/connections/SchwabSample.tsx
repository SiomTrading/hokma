import { useState } from "react";
import { Button, IconButton, Loading, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { connectionErrorText, sampleApi } from "./api";
import type { SampleAnswer, SampleProvider } from "./types";

/** "Save an anonymous sample of the account" (SPEC-026 3390e1). One button: Hokma asks Schwab once for the balances and positions of the accounts and
 *  saves the answer in the downloads folder. Account numbers and ids are XXXX in the file; the amounts are intact. It reads only. */
export function SchwabSampleDialog({ onClose, provider = "schwab" }: { onClose: () => void; provider?: SampleProvider }) {
  const { t } = useI18n();
  const alpaca = provider === "alpaca";
  const [phase, setPhase] = useState<"idle" | "running" | "done" | "error">("idle");
  const [answer, setAnswer] = useState<SampleAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = () => {
    setPhase("running");
    setError(null);
    sampleApi.save(provider).then(
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
    <Modal label={t(alpaca ? "connections.alpaca.sample" : "connections.schwab.sample")} onClose={onClose} className="conn-dialog">
      <div className="conn-dialog-body" data-testid={alpaca ? "alpaca-sample" : "schwab-sample"}>
        <header className="conn-dialog-head">
          <h2 className="h1">{t(alpaca ? "connections.alpaca.sample" : "connections.schwab.sample")}</h2>
          <IconButton icon="close" label={t("connections.add.close")} onClick={onClose} />
        </header>
        <p className="p">{t(alpaca ? "connections.alpaca.sample.what" : "connections.schwab.sample.what")}</p>
        <p className="p conn-note">{t("connections.schwab.sample.safe")}</p>
        {phase === "running" ? <Loading label={t(alpaca ? "connections.alpaca.sample.running" : "connections.schwab.sample.running")} /> : null}
        {phase === "error" ? (
          <p className="conn-error" role="alert" data-testid="schwab-sample-error">
            {error}
          </p>
        ) : null}
        {phase === "done" && answer ? (
          <div role="status" data-testid="schwab-sample-result">
            <p className="p">{t(answer.folder_is_downloads ? "connections.schwab.sample.saved" : "connections.schwab.sample.savedElsewhere")}</p>
            <p className="mono" data-testid="schwab-sample-file">
              {answer.file}
            </p>
            <p className="p">
              {alpaca
                ? t("connections.alpaca.sample.counts", { positions: answer.positions })
                : t("connections.schwab.sample.counts", { accounts: answer.accounts, positions: answer.positions })}
            </p>
            {alpaca ? null : (
              <p className="p" data-testid="schwab-sample-activity">
                {t(answer.activity ? "connections.schwab.sample.activityYes" : "connections.schwab.sample.activityNo")}
              </p>
            )}
            <p className="p">{t("connections.schwab.sample.send")}</p>
          </div>
        ) : null}
        <Button variant="primary" disabled={phase === "running"} onClick={run} data-testid="schwab-sample-run">
          {phase === "done" || phase === "error" ? t("connections.schwab.sample.again") : t("connections.schwab.sample.run")}
        </Button>
      </div>
    </Modal>
  );
}
