import { useI18n } from "@/i18n";
import { DeskEmptyState } from "../EmptyState";
import type { TabProps } from "./types";

/** Rules: rules are written by talking with the desk chief. */
export function RulesTab(_props: TabProps) {
  const { t } = useI18n();
  return <DeskEmptyState icon="shield" title={t("desks.rules.title")} text={t("desks.rules.text")} />;
}
