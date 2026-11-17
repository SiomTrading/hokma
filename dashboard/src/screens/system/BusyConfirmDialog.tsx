import { Button, Modal } from "@/design";
import { useI18n } from "@/i18n";

interface BusyConfirmDialogProps {
  /** Accessible label of the dialog: what the owner is about to do ("Update", "Restore"...). */
  title: string;
  onContinue: () => void;
  onCancel: () => void;
}

/** "An agent is working, it will be cut off": shown when the engine refused an action with 409 `*.in_use`. */
export function BusyConfirmDialog({ title, onContinue, onCancel }: BusyConfirmDialogProps) {
  const { t } = useI18n();
  return (
    <Modal label={title} role="alertdialog" onClose={onCancel} style={{ maxWidth: 420, padding: "20px 22px" }}>
      <h2 className="h2">{t("busy.title")}</h2>
      <p className="p" style={{ marginTop: 8 }} data-testid="busy-warning">{t("busy.warning")}</p>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <Button size="sm" onClick={onCancel}>{t("busy.keep")}</Button>
        <Button size="sm" variant="primary" onClick={onContinue} data-testid="busy-continue">{t("busy.continue")}</Button>
      </div>
    </Modal>
  );
}
