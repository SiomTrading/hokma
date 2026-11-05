import { registerScreen, registerSettingsSection, registerSlot } from "@/shell/registry";
import { AgentsSettings } from "./AgentsSettings";
import { ModelsSettings } from "./ModelsSettings";
import { TasksScreen } from "./TasksScreen";
import { UsageStatusItem } from "./UsageStatusItem";

registerSettingsSection({ id: "agents", component: AgentsSettings });
registerSettingsSection({ id: "models", component: ModelsSettings });
registerScreen({ id: "tasks", component: TasksScreen });
registerSlot("statusBar", UsageStatusItem, { order: 20 });
