import { useI18n } from "@/i18n";
import { DeskEmptyState } from "../EmptyState";
import type { TabProps } from "./types";

/** Rules: risk rules in code arrive in step 3, so the tab says what arrives and when. */
export function RulesTab(_props: TabProps) {
  const { t } = useI18n();
  return <DeskEmptyState icon="shield" title={t("desks.rules.title")} text={t("desks.rules.text")} when={t("desks.rules.when")} />;
}
