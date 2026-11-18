import { Pane } from "@/design";
import { useI18n } from "@/i18n";
import { DeskEmptyState } from "./EmptyState";

/** The bottom panel of the terminal. No simulated operations exist yet, so it says what will fill it and when. */
export function PositionsEmpty() {
  const { t } = useI18n();
  return (
    <Pane className="dterm-positions" aria-label={t("desks.terminal.positions")}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{t("desks.terminal.positions")}</span>
      </div>
      <div className="dterm-positions-body">
        <DeskEmptyState icon="chart" title={t("desks.terminal.positions.title")} text={t("desks.terminal.positions.text")} when={t("desks.terminal.positions.when")} />
      </div>
    </Pane>
  );
}
