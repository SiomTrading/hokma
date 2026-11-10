import { Button, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { ClaudeStep } from "../first-run/ClaudeStep";
import { ConnectCards } from "./ConnectCards";
import { useEngines } from "./useEngines";

/** First-run step "Connect your AI". Claude Code first and recommended, then the other ways in. Never mandatory. */
export function EnginesStep({ pollMs, onLater }: { pollMs?: number; onLater: () => void }) {
  const { t } = useI18n();
  const { data, reload } = useEngines();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <p className="p">{t("eng.lead")}</p>
      <div className="pane" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        <Pill tone="ok" style={{ alignSelf: "flex-start" }}>
          {t("eng.recommended")}
        </Pill>
        <ClaudeStep pollMs={pollMs} />
      </div>
      <ConnectCards listing={data} onChanged={() => void reload()} />
      <Button variant="ghost" style={{ alignSelf: "flex-start" }} onClick={onLater}>
        {t("eng.later")}
      </Button>
    </div>
  );
}
