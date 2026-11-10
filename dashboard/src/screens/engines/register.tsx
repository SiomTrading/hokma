import { registerSettingsSection } from "@/shell/registry";
import { EnginesSettings } from "./EnginesSettings";

registerSettingsSection({ id: "engines", component: EnginesSettings, titleKey: "settings.engines", icon: "connections", order: 2.5 });
