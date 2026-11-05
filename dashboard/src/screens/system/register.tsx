import { registerSettingsSection, registerSlot } from "@/shell/registry";
import { ExtensionsSection } from "./ExtensionsSection";
import { NewsSection } from "./NewsSection";
import { StatusSection } from "./StatusSection";
import { VersionItem } from "./VersionItem";

registerSettingsSection({ id: "extensions", component: ExtensionsSection });
registerSettingsSection({ id: "status", component: StatusSection });
registerSettingsSection({ id: "news", component: NewsSection });
registerSlot("statusBar.end", VersionItem, { order: 100 });
