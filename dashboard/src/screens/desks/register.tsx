import { registerScreen, registerSlot } from "@/shell/registry";
import { t } from "@/i18n";
import { DesksScreen } from "./DesksScreen";
import { SidebarDesks } from "./SidebarDesks";
import { getSnapshot } from "./store";

// The screen meta (title, icon) is built in: see BUILTIN_SCREENS. Each desk opens in its own tab, keyed by its id.
registerScreen({
  id: "desks",
  component: DesksScreen,
  title: (params) => getSnapshot().desks.find((desk) => String(desk.id) === params.desk)?.name ?? t("screen.desks"),
});
registerSlot("sidebar.desks", SidebarDesks, { order: 0 });
