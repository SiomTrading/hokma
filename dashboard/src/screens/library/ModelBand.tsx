import { Button, Icon, Meter } from "@/design";
import { useI18n } from "@/i18n";
import { useModelStatus } from "./useModelStatus";

/**
 * State of the search, with nothing to press: a discreet bar while the app prepares it on its own, one button to
 * try again if that failed, and nothing at all once it is ready.
 */
export function ModelBand({ pollMs }: { pollMs?: number }) {
  const { t } = useI18n();
  const { status, retry } = useModelStatus(pollMs);

  if (!status || status.state === "ready") return null;

  if (status.state === "failed") {
    return (
      <div className="lib-band bad" role="alert" data-testid="library-model-failed">
        <Icon name="search" size={14} />
        <span className="lib-band-text">{t("library.model.failed")}</span>
        <Button size="sm" onClick={() => void retry()}>
          {t("library.model.retry")}
        </Button>
      </div>
    );
  }

  return (
    <div className="lib-band" role="status" data-testid="library-model-preparing">
      <Icon name="search" size={14} />
      <span className="lib-band-text">{t("library.model.preparing")}</span>
      <div className="lib-band-bar">
        <Meter value={status.percent} label={t("library.model.progress")} />
      </div>
    </div>
  );
}
