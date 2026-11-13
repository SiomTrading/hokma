import { registerSettingsSection } from "@/shell/registry";
import { BackupSection } from "./BackupSection";
import { PersistentSection } from "./PersistentSection";
import { TelegramSection } from "./TelegramSection";

registerSettingsSection({ id: "persistent", component: PersistentSection, titleKey: "settings.persistent", icon: "power", order: 4 });
registerSettingsSection({ id: "telegram", component: TelegramSection, titleKey: "settings.telegram", icon: "send", order: 4.5 });
registerSettingsSection({ id: "backup", component: BackupSection, titleKey: "settings.backup", icon: "shield", order: 4.7 });
