import { registerPanelSection } from "@/shell/registry";
import { AgentsSection, useAgentsSummary } from "./AgentsSection";
import { BackgroundSection, useBackgroundSummary } from "./BackgroundSection";
import { ResultsSection, useResultsSummary } from "./ResultsSection";

registerPanelSection({ id: "agents", order: 10, titleKey: "rightPanel.section.agents", component: AgentsSection, useSummary: useAgentsSummary });
registerPanelSection({ id: "background", order: 20, titleKey: "rightPanel.section.background", component: BackgroundSection, useSummary: useBackgroundSummary });
registerPanelSection({ id: "results", order: 30, titleKey: "rightPanel.section.results", component: ResultsSection, useSummary: useResultsSummary });
