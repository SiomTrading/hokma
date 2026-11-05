import { useRef, useState } from "react";
import { Icon, IconButton, Menu, MenuItem, Pill, Tab } from "@/design";
import { useI18n } from "@/i18n";
import { getScreen } from "./registry";
import { ADD_MENU_SCREENS, BUILTIN_SCREENS, DOT_COLOR, screenMeta } from "./screens";
import { Slot, hasSlot } from "./Slot";
import { useShell } from "./store";
import type { Tab as TabModel } from "./store";

export function useTabTitle(): (tab: TabModel) => string {
  const { t } = useI18n();
  return (tab) => {
    if (tab.title) return tab.title;
    const custom = getScreen(tab.screen)?.title?.(tab.params, t);
    if (custom) return custom;
    const meta = screenMeta(tab.screen);
    return meta ? t(meta.titleKey) : tab.screen;
  };
}

export function TopBar() {
  const { t } = useI18n();
  const shell = useShell();
  const [addOpen, setAddOpen] = useState(false);
  const addRef = useRef<HTMLButtonElement>(null);
  const tabTitle = useTabTitle();
  const { layout } = shell;

  const pick = (screen: string) => {
    shell.openScreen(screen);
    setAddOpen(false);
  };

  return (
    <div className="top">
      <IconButton icon="sidebar" label={t("top.sidebar")} on={layout.sidebar === 0} aria-pressed={undefined} onClick={shell.toggleSidebar} />
      <div className="top-tabs">
        {shell.tabs.map((tab) => {
          const meta = screenMeta(tab.screen);
          const title = tabTitle(tab);
          return (
            <Tab
              key={tab.key}
              label={title}
              active={tab.key === shell.activeKey}
              dotColor={DOT_COLOR[meta?.dot ?? "muted"]}
              onSelect={() => shell.activateTab(tab.key)}
              onClose={() => shell.closeTab(tab.key)}
              closeLabel={t("top.closeTab", { title })}
            />
          );
        })}
        <button
          ref={addRef}
          type="button"
          className={addOpen ? "ib on" : "ib"}
          aria-label={t("top.addTab")}
          aria-haspopup="menu"
          aria-expanded={addOpen}
          onClick={() => setAddOpen((v) => !v)}
        >
          <Icon name="plus" size={15} strokeWidth={2} />
        </button>
      </div>
      <Menu open={addOpen} onClose={() => setAddOpen(false)} label={t("top.addTab")} placement="below" triggerRef={addRef}>
        <div style={{ padding: "8px 10px 4px" }}>
          <span className="lbl">{t("addMenu.modules")}</span>
        </div>
        {ADD_MENU_SCREENS.map((id) => {
          const meta = BUILTIN_SCREENS[id]!;
          return (
            <MenuItem key={id} dotColor="var(--acc)" onClick={() => pick(id)}>
              {t(meta.menuKey ?? meta.navKey)}
            </MenuItem>
          );
        })}
        {hasSlot("addMenu.assets") ? (
          <>
            <div className="sep" />
            <div style={{ padding: "8px 10px 4px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span className="lbl">{t("addMenu.assets")}</span>
              <Pill style={{ height: 20, fontSize: 10 }}>{t("screen.chart")}</Pill>
            </div>
            <Slot name="addMenu.assets" close={() => setAddOpen(false)} />
          </>
        ) : null}
      </Menu>
      <IconButton icon="terminal" label={t("top.terminal")} on={layout.terminal} aria-pressed={undefined} onClick={shell.toggleTerminal} />
      <IconButton icon="rightPanel" label={t("top.rightPanel")} on={layout.rightPanel} aria-pressed={undefined} onClick={shell.toggleRightPanel} />
      <IconButton icon="fullscreen" label={t("top.fullscreen")} on={layout.focus} aria-pressed={undefined} onClick={shell.toggleFocus} />
    </div>
  );
}
