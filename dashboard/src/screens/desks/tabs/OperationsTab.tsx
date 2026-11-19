import { useI18n } from "@/i18n";
import { DeskEmptyState } from "../EmptyState";
import type { TabProps } from "./types";

/** Operations: there are no trades yet, so the tab says what it will hold. */
export function OperationsTab(_props: TabProps) {
  const { t } = useI18n();
  return <DeskEmptyState icon="chart" title={t("desks.operations.title")} text={t("desks.operations.text")} />;
}
