import { useI18n } from "@/i18n";
import { DeskEmptyState } from "../EmptyState";
import type { TabProps } from "./types";

/** Operations: no simulated operations exist in step 1, so the tab says what arrives and when. */
export function OperationsTab(_props: TabProps) {
  const { t } = useI18n();
  return <DeskEmptyState icon="chart" title={t("desks.operations.title")} text={t("desks.operations.text")} when={t("desks.operations.when")} />;
}
