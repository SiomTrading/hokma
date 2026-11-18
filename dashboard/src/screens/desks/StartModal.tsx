import { Modal } from "@/design";
import { useI18n } from "@/i18n";

interface Props {
  onClose: () => void;
  onHokma: () => void;
  onTemplate: () => void;
}

/** "How do you want to create the desk?": two big paths, ask Hokma (recommended) or start from a template. */
export function StartModal({ onClose, onHokma, onTemplate }: Props) {
  const { t } = useI18n();
  return (
    <Modal label={t("desks.start.title")} onClose={onClose} style={{ width: "min(760px, 94vw)", flexDirection: "column" }}>
      <div className="desk-modal-head">
        <h2 className="h1">{t("desks.start.title")}</h2>
        <button type="button" className="ib" aria-label={t("desks.close")} onClick={onClose}>
          ✕
        </button>
      </div>
      <div className="desk-start-grid">
        <button type="button" className="opt desk-start-opt" onClick={onHokma}>
          <span className="desk-start-art desk-start-art-hokma" aria-hidden="true" />
          <span className="desk-start-text">
            <b className="desk-start-name">
              {t("desks.start.hokma.name")}
              <span className="pill new">{t("desks.start.recommended")}</span>
            </b>
            <span className="desk-start-desc">{t("desks.start.hokma.text")}</span>
            <span className="desk-start-note">{t("desks.start.hokma.note")}</span>
          </span>
        </button>
        <button type="button" className="opt desk-start-opt" onClick={onTemplate}>
          <span className="desk-start-art desk-start-art-template" aria-hidden="true">
            <i /><i /><i /><i /><i /><i />
          </span>
          <span className="desk-start-text">
            <b className="desk-start-name">{t("desks.start.template.name")}</b>
            <span className="desk-start-desc">{t("desks.start.template.text")}</span>
          </span>
        </button>
      </div>
    </Modal>
  );
}
