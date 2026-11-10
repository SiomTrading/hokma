import { Button } from "@/design";
import { useI18n } from "@/i18n";

/** The provider refuses the location. It states the fact and offers another engine. It never suggests a VPN. */
export function RegionCard({ provider, onRetry, onOther }: { provider: string; onRetry?: () => void; onOther: () => void }) {
  const { t } = useI18n();
  return (
    <div className="pane" role="alert" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
      <h3 className="h2">{t("eng.region.title", { provider })}</h3>
      <p className="p">{t("eng.region.body", { provider })}</p>
      <div style={{ display: "flex", gap: 8 }}>
        {onRetry ? (
          <Button size="sm" onClick={onRetry}>
            {t("eng.region.retry")}
          </Button>
        ) : null}
        <Button size="sm" variant="primary" onClick={onOther}>
          {t("eng.region.other")}
        </Button>
      </div>
    </div>
  );
}
