import { useEffect } from "react";
import { Button, EmptyState, IconButton, Isotype, Pane, Toaster, cx } from "@/design";
import { useI18n } from "@/i18n";
import { getScreen } from "./registry";
import { SettingsModal } from "./SettingsModal";
import { Sidebar } from "./Sidebar";
import { Boundary, Slot, hasSlot } from "./Slot";
import { TopBar } from "./TopBar";
import { matchShortcut } from "./shortcuts";
import { useShell } from "./store";

function ScreenHost() {
  const { t } = useI18n();
  const shell = useShell();

  if (shell.activeKey === null) {
    return (
      <Pane as="section" style={{ flex: 1, display: "grid", placeItems: "center", textAlign: "center", padding: 24 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "center" }}>
          <p className="disp">{t("empty.title")}</p>
          <p className="p">{t("empty.body")}</p>
          <Button variant="primary" onClick={shell.newConversation}>
            {t("nav.newChat")}
          </Button>
        </div>
      </Pane>
    );
  }

  return (
    <>
      {shell.tabs.map((tab) => {
        const reg = getScreen(tab.screen);
        const active = tab.key === shell.activeKey;
        if (!active && !reg?.keepAlive) return null;
        const Screen = reg?.component;
        return (
          <div key={tab.key} className="screen-host" hidden={!active} data-screen={tab.screen}>
            <Boundary fallback={<EmptyState icon="close" title={t("screen.unavailable.title")} description={t("screen.unavailable.body")} />}>
              {Screen ? (
                <Screen tabKey={tab.key} params={tab.params} active={active} />
              ) : (
                <Pane as="section" style={{ flex: 1, display: "grid", placeItems: "center" }}>
                  <EmptyState title={t("screen.unavailable.title")} description={t("screen.unavailable.body")} />
                </Pane>
              )}
            </Boundary>
          </div>
        );
      })}
    </>
  );
}

function RightPanel() {
  const { t } = useI18n();
  const shell = useShell();
  return (
    <aside className="right" aria-label={t("rightPanel.title")}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 10px 6px 14px" }}>
        <span className="sec" style={{ padding: 0 }}>
          {t("rightPanel.title")}
        </span>
        <div style={{ display: "flex", gap: 2 }}>
          <Slot name="rightPanel.actions" />
          <IconButton icon="chevronRight" size={15} label={t("rightPanel.collapse")} onClick={shell.toggleRightPanel} />
        </div>
      </div>
      {hasSlot("rightPanel") ? (
        <Slot name="rightPanel" />
      ) : (
        <p className="p" style={{ padding: "0 14px" }}>
          {t("rightPanel.unavailable")}
        </p>
      )}
    </aside>
  );
}

function TerminalDock() {
  const { t } = useI18n();
  const shell = useShell();
  return (
    <div className="term" role="region" aria-label={t("terminal.title")}>
      <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "4px 8px", borderBottom: "1px solid var(--border)" }}>
        <span className="lbl" style={{ padding: "0 6px" }}>
          {t("terminal.title")}
        </span>
        <IconButton icon="plus" size={14} label={t("terminal.new")} style={{ marginLeft: "auto" }} onClick={() => shell.emit("terminal:new")} />
        <IconButton icon="chevronDown" size={14} label={t("terminal.close")} onClick={shell.toggleTerminal} />
      </div>
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {hasSlot("terminal") ? (
          <Slot name="terminal" />
        ) : (
          <p className="p" style={{ padding: "10px 14px" }}>
            {t("terminal.unavailable")}
          </p>
        )}
      </div>
    </div>
  );
}

function useGlobalShortcuts(): void {
  const shell = useShell();
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const id = matchShortcut(event);
      if (!id) return;
      event.preventDefault();
      switch (id) {
        case "newChat":
          shell.newConversation();
          break;
        case "search":
          shell.focusSearch();
          break;
        case "terminal":
          shell.toggleTerminal();
          break;
        case "sidebar":
          shell.toggleSidebar();
          break;
        case "rightPanel":
          shell.toggleRightPanel();
          break;
        case "fullscreen":
          shell.toggleFocus();
          break;
        case "settings":
          shell.openSettings();
          break;
        default:
          break;
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [shell]);
}

export function Shell() {
  const { t } = useI18n();
  const shell = useShell();
  const { layout } = shell;
  useGlobalShortcuts();

  useEffect(() => {
    document.documentElement.dataset.theme = shell.theme;
    document.documentElement.style.colorScheme = shell.theme;
  }, [shell.theme]);

  const rootClass = cx(
    "hk",
    shell.theme === "light" && "light",
    layout.sidebar === 1 && "sbc",
    layout.sidebar === 2 && "sbx",
    !layout.rightPanel && "rpoff",
    layout.focus && "focus",
  );

  return (
    <div className={rootClass} data-testid="shell">
      <div className="blob" aria-hidden="true" style={{ width: 640, height: 640, background: "var(--blob1)", top: -280, left: "8%", opacity: 0.75 }} />
      <div className="blob" aria-hidden="true" style={{ width: 520, height: 520, background: "var(--blob2)", bottom: -260, right: -140, opacity: 0.55 }} />
      <div className="app">
        <TopBar />
        <Sidebar />
        <main className="center">
          <ScreenHost />
        </main>
        {layout.rightPanel && !layout.focus ? <RightPanel /> : null}
        {layout.terminal && !layout.focus ? <TerminalDock /> : null}
        <div className="status" role="contentinfo" aria-label={t("settings.status")}>
          <Slot name="statusBar" />
          <span className="end">
            <Slot name="statusBar.end" />
          </span>
        </div>
      </div>
      {shell.settings.open ? <SettingsModal /> : null}
      <Slot name="overlay.firstRun" />
      <Toaster items={shell.toasts} onDismiss={shell.dismissToast} dismissLabel={t("toast.close")} />
    </div>
  );
}

export function NoToken() {
  const { t } = useI18n();
  return (
    <div className="hk" data-testid="no-token">
      <div className="blob" aria-hidden="true" style={{ width: 640, height: 640, background: "var(--blob1)", top: -280, left: "8%", opacity: 0.75 }} />
      <div className="fullpage" style={{ position: "relative" }}>
        <main style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 14, maxWidth: 460 }}>
          <Isotype size={56} />
          <h1 className="disp">{t("noToken.title")}</h1>
          <p className="p">{t("noToken.body")}</p>
        </main>
      </div>
    </div>
  );
}
