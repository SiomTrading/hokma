import { registerSettingsSection } from "@/shell/registry";
import { BackupSection } from "./BackupSection";
import { MailSection } from "./MailSection";
import { PersistentSection } from "./PersistentSection";
import { TelegramSection } from "./TelegramSection";

registerSettingsSection({ id: "persistent", component: PersistentSection, titleKey: "settings.persistent", icon: "power", order: 4 });
registerSettingsSection({ id: "telegram", component: TelegramSection, titleKey: "settings.telegram", icon: "send", order: 4.5 });
registerSettingsSection({ id: "mail", component: MailSection, titleKey: "settings.mail", icon: "send", order: 4.6 });
registerSettingsSection({ id: "backup", component: BackupSection, titleKey: "settings.backup", icon: "shield", order: 4.7 });
