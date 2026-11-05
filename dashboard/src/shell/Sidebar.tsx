import { Input, Isotype, NavItem } from "@/design";
import { useI18n } from "@/i18n";
import { SIDEBAR_SCREENS, BUILTIN_SCREENS } from "./screens";
import { Slot } from "./Slot";
import { formatShortcut, getShortcut } from "./shortcuts";
import { useShell } from "./store";

export function Sidebar() {
  const { t } = useI18n();
  const shell = useShell();
  const activeScreen = shell.tabs.find((tab) => tab.key === shell.activeKey)?.screen;

  return (
    <aside className="side" aria-label={t("nav.menu")}>
      <div className="brand">
        <Isotype size={30} />
        <div className="hide-c" style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span className="brand-name">HOKMA</span>
          <span className="brand-sub">
            <Slot name="brand.subtitle" />
          </span>
        </div>
      </div>
      <NavItem icon="plus" label={t("nav.newChat")} kbd={formatShortcut(getShortcut("newChat"))} onClick={shell.newConversation} />
      <div className="sep" />
      {SIDEBAR_SCREENS.map((id) => {
        const meta = BUILTIN_SCREENS[id]!;
        return <NavItem key={id} icon={meta.icon} label={t(meta.navKey)} active={activeScreen === id} onClick={() => shell.openScreen(id)} />;
      })}
      <div className="sep" />
      <div className="hide-c side-scroll">
        <label htmlFor="sidebar-search" className="sr-only">
          {t("sidebar.search")}
        </label>
        <Input
          id="sidebar-search"
          type="search"
          style={{ minHeight: 34, marginBottom: 4, fontSize: 12 }}
          placeholder={t("sidebar.search")}
          value={shell.sidebarQuery}
          onChange={(event) => shell.setSidebarQuery(event.target.value)}
        />
        <Slot name="sidebar.conversations" query={shell.sidebarQuery} />
      </div>
      <div className="sep" />
      <NavItem icon="learn" iconSize={16} label={t("nav.learn")} active={activeScreen === "learn"} onClick={() => shell.openScreen("learn")} />
      <NavItem icon="settings" iconSize={16} label={t("nav.settings")} onClick={() => shell.openSettings()} />
    </aside>
  );
}
