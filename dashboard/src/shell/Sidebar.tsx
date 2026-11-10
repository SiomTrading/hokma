import { useRef, useState } from "react";
import { isFirefox, useSessionInfo } from "@/api";
import { IconButton, Input, Isotype, NavItem, NavLink } from "@/design";
import { useI18n } from "@/i18n";
import { ShutdownDialog } from "@/screens/system/ShutdownDialog";
import { SIDEBAR_SCREENS, BUILTIN_SCREENS } from "./screens";
import { Slot } from "./Slot";
import { formatShortcut, formatShortcutCompact, getShortcut, isMac } from "./shortcuts";
import { useSidebarDensity } from "./sidebarDensity";
import { isPlainClick } from "./links";
import { useShell } from "./store";

export function Sidebar() {
  const { t } = useI18n();
  const shell = useShell();
  const [confirmingShutdown, setConfirmingShutdown] = useState(false);
  const session = useSessionInfo();
  // A new browser tab enters by itself only where Hokma remembers the session (not Firefox, not private mode).
  const canOpenInTab = !isFirefox() && !session.storageUnavailable;
  const activeScreen = shell.tabs.find((tab) => tab.key === shell.activeKey)?.screen;
  const sideRef = useRef<HTMLElement>(null);
  const density = useSidebarDensity(sideRef);

  return (
    <aside ref={sideRef} className="side" data-density={density} aria-label={t("nav.menu")}>
      <div className="brand">
        <Isotype size={30} className="brand-mark" />
        <div className="hide-c" style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span className="brand-name">HOKMA</span>
          <span className="brand-sub">
            <Slot name="brand.subtitle" />
          </span>
        </div>
        <IconButton
          icon="sidebar"
          className="brand-toggle"
          label={t("sidebar.collapse")}
          title={`${t("sidebar.collapse")} (${formatShortcutCompact(getShortcut("sidebar"))})`}
          onClick={shell.toggleSidebar}
        />
      </div>
      <NavLink
        icon="plus"
        label={t("nav.newChat")}
        href="#/chat/new"
        title={t(canOpenInTab ? "nav.newChatTitle" : "nav.newChatTitlePlain", { mod: isMac() ? "Cmd" : "Ctrl" })}
        kbd={formatShortcut(getShortcut("newChat"))}
        onClick={(event) => {
          if (!isPlainClick(event)) return; // wheel, Ctrl, Cmd or Shift: the browser opens it in another tab
          event.preventDefault();
          shell.newConversation();
        }}
      />
      <div className="sep" />
      {SIDEBAR_SCREENS.map((id) => {
        const meta = BUILTIN_SCREENS[id]!;
        return <NavItem key={id} icon={meta.icon} label={t(meta.navKey)} active={activeScreen === id} onClick={() => shell.openScreen(id)} />;
      })}
      <div className="sep side-recents-sep" />
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
      <NavItem icon="power" iconSize={16} label={t("nav.shutdown")} title={t("nav.shutdown")} onClick={() => setConfirmingShutdown(true)} />
      {confirmingShutdown ? (
        <ShutdownDialog
          withSession
          onClose={() => setConfirmingShutdown(false)}
          onError={(text) => shell.toast({ tone: "bad", text })}
        />
      ) : null}
    </aside>
  );
}
