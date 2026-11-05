import { Button, Modal } from "@/design";
import { useI18n } from "@/i18n";

interface Props {
  onConfirm: () => void;
  onCancel: () => void;
}

/** Asked before any agent is set to Opus: it spends the weekly limit much faster than Sonnet. */
export function OpusConfirm({ onConfirm, onCancel }: Props) {
  const { t } = useI18n();
  return (
    <Modal label={t("agents.opus.title")} onClose={onCancel} style={{ width: "min(440px, 94vw)" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: 22 }}>
        <h2 className="h1">{t("agents.opus.title")}</h2>
        <p className="p">{t("agents.opus.warning")}</p>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button variant="ghost" onClick={onCancel}>{t("agents.opus.cancel")}</Button>
          <Button variant="primary" onClick={onConfirm}>{t("agents.opus.confirm")}</Button>
        </div>
      </div>
    </Modal>
  );
}
