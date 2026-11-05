import { Button, Modal } from "@/design";
import { useI18n } from "@/i18n";

interface Props {
  /** The backend's own words (already in the owner's language). */
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Asked only after the backend said the destination already holds data. */
export function ReplaceConfirm({ message, onConfirm, onCancel }: Props) {
  const { t } = useI18n();
  return (
    <Modal label={t("firstrun.db.replaceTitle")} onClose={onCancel} closeOnScrim={false} style={{ flexDirection: "column", width: "min(520px, 100%)" }}>
      <div style={{ padding: 22, display: "flex", flexDirection: "column", gap: 10 }}>
        <h2 className="h1">{t("firstrun.db.replaceTitle")}</h2>
        <p className="p">{message}</p>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "14px 22px", borderTop: "1px solid var(--border)" }}>
        <Button variant="ghost" onClick={onCancel}>
          {t("firstrun.cancel")}
        </Button>
        <Button variant="danger" onClick={onConfirm}>
          {t("firstrun.db.replaceConfirm")}
        </Button>
      </div>
    </Modal>
  );
}
