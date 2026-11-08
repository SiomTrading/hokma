import { registerSettingsSection } from "@/shell/registry";
import { TrashSection } from "./TrashSection";

registerSettingsSection({ id: "trash", component: TrashSection, titleKey: "settings.trash", icon: "trash", order: 3.5 });
