import { EmptyState, Icon, Pane } from "@/design";
import { useI18n } from "@/i18n";

interface Props {
  folded: boolean;
  onToggle: () => void;
}

/** The bottom panel of the terminal: the open positions of the desk, or a plain note when there are none. It folds down. */
export function PositionsEmpty({ folded, onToggle }: Props) {
  const { t } = useI18n();
  return (
    <Pane className="dterm-positions" aria-label={t("desks.terminal.positions")}>
      <div className="dterm-ph">
        <span className="dterm-tab on">{t("desks.terminal.positions")}</span>
        <span className="dterm-ph-end">
          <button
            type="button"
            className="ib dterm-fold"
            aria-expanded={!folded}
            aria-label={t(folded ? "desks.terminal.positions.unfold" : "desks.terminal.positions.fold")}
            onClick={onToggle}
          >
            <Icon name={folded ? "chevronUp" : "chevronDown"} size={16} />
          </button>
        </span>
      </div>
      {folded ? null : (
        <div className="dterm-positions-body">
          <EmptyState icon="chart" title={t("desks.terminal.positions.title")} />
        </div>
      )}
    </Pane>
  );
}
