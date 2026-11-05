import { registerSlot } from "@/shell/registry";
import { CreateProjectButton, ProjectsPanel } from "./ProjectsPanel";

registerSlot("rightPanel", ProjectsPanel, { order: 0 });
registerSlot("rightPanel.actions", CreateProjectButton, { order: 0 });
