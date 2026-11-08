import { useState } from "react";
import { ApiError, shutdownFromEntry, shutdownHokma } from "@/api";
import { Button, Modal } from "@/design";
import { useI18n } from "@/i18n";

interface ShutdownDialogProps {
  /** True from the panel (the session token travels); false from the entry screen of a browser without a session. */
  withSession: boolean;
  onClose: () => void;
  /** The call failed: the panel shows a toast, the entry screen shows the text under the question. */
  onError?: (message: string) => void;
}

/** The short question before shutting Hokma down. Escape and Cancel close it without calling anything. */
export function ShutdownDialog({ withSession, onClose, onError }: ShutdownDialogProps) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    try {
      await (withSession ? shutdownHokma() : shutdownFromEntry());
    } catch (error) {
      const message = error instanceof ApiError && error.message ? error.message : t("shutdown.failed");
      if (onError) {
        onError(message);
        onClose();
      } else {
        setProblem(message);
        setBusy(false);
      }
    }
  };

  return (
    <Modal label={t("shutdown.confirmTitle")} role="alertdialog" onClose={onClose} style={{ maxWidth: 420, padding: "20px 22px" }}>
      <h2 className="h2">{t("shutdown.confirmTitle")}</h2>
      <p className="p" style={{ marginTop: 8 }}>
        {t("shutdown.confirmBody")}
      </p>
      {problem ? (
        <p className="p" role="alert" data-testid="shutdown-problem" style={{ marginTop: 8 }}>
          {problem}
        </p>
      ) : null}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <Button size="sm" onClick={onClose} disabled={busy}>
          {t("shutdown.cancel")}
        </Button>
        <Button size="sm" variant="primary" onClick={() => void confirm()} disabled={busy} data-testid="shutdown-confirm">
          {t("shutdown.confirm")}
        </Button>
      </div>
    </Modal>
  );
}
