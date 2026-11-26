import { useSyncExternalStore } from "react";
import { useI18n } from "@/i18n";
import { registerPanelSection } from "@/shell/registry";
import { CreateProjectButton, ProjectsPanel } from "./ProjectsPanel";
import { getSnapshot, subscribe } from "./store";

/** What the folded section says: the active project, or that there is none. */
function useProjectSummary(): string {
  const { t } = useI18n();
  const { list } = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const active = list?.projects.find((project) => project.id === list.active_id);
  return active ? active.name : t("rightPanel.noProject");
}

registerPanelSection({
  id: "project",
  order: 0,
  fill: true,
  titleKey: "rightPanel.section.project",
  component: ProjectsPanel,
  actions: CreateProjectButton,
  useSummary: useProjectSummary,
});
