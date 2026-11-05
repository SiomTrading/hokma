import { useEffect, useRef, useSyncExternalStore } from "react";
import "@xterm/xterm/css/xterm.css";
import { Button, EmptyState, Tab } from "@/design";
import { useI18n } from "@/i18n";
import { useShell, useShellEvent } from "@/shell/useShell";
import "./terminal.css";
import { activate, applyTheme, closeTerminal, getSnapshot, hostOf, launchTerminal, setActiveProject, setThemeSource, subscribe, takeAutoStart } from "./store";
import type { TermStatus } from "./types";

const DOT: Record<TermStatus, string> = {
  connecting: "var(--warn)",
  ready: "var(--ok)",
  exited: "var(--t3)",
  error: "var(--bad)",
  closed: "var(--t3)",
};

export function TerminalPanel() {
  const { t } = useI18n();
  const shell = useShell();
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const area = useRef<HTMLDivElement>(null);

  useShellEvent("terminal:new", () => {
    launchTerminal();
  });
  useShellEvent("project:active", (payload) => {
    const id = payload && typeof payload === "object" && "id" in payload ? (payload as { id: unknown }).id : null;
    setActiveProject(id === null || id === undefined ? null : String(id));
  });

  // Colors follow the theme of the app root. Declared first: it must exist before the first terminal is created.
  useEffect(() => {
    setThemeSource(document.querySelector(".hk"));
    const frame = requestAnimationFrame(applyTheme);
    return () => cancelAnimationFrame(frame);
  }, [shell.theme]);

  // The first time the dock opens it starts one terminal; closing them all leaves the empty state.
  useEffect(() => {
    if (takeAutoStart()) launchTerminal();
  }, []);

  // Show the active session and keep it fitted to the dock.
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    const current = hostOf(snap.activeId);
    el.replaceChildren(...(current ? [current.host] : []));
    if (!current) return;
    const refit = () => requestAnimationFrame(current.fit);
    refit();
    current.focus();
    const observer = new ResizeObserver(refit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [snap.activeId, snap.tabs.length]);

  if (snap.tabs.length === 0) {
    return (
      <EmptyState
        title={t("term.empty.title")}
        description={t("term.empty.description")}
        action={
          <Button size="sm" onClick={() => launchTerminal()}>
            {t("term.empty.action")}
          </Button>
        }
      />
    );
  }

  return (
    <div className="term-panel">
      {snap.tabs.length > 1 ? (
        <div className="term-tabs" role="tablist" aria-label={t("term.tabs")}>
          {snap.tabs.map((tab) => (
            <Tab
              key={tab.id}
              label={t("term.tab", { n: String(tab.n) })}
              active={tab.id === snap.activeId}
              dotColor={DOT[tab.status]}
              onSelect={() => activate(tab.id)}
              onClose={() => closeTerminal(tab.id)}
              closeLabel={t("term.tabClose", { n: String(tab.n) })}
            />
          ))}
        </div>
      ) : null}
      <div className="term-area" ref={area} role="group" aria-label={t("term.screen", { n: String(snap.tabs.find((x) => x.id === snap.activeId)?.n ?? 1) })} />
      {snap.tabs.length === 1 ? (
        <button type="button" className="term-close-single" aria-label={t("term.tabClose", { n: String(snap.tabs[0].n) })} onClick={() => closeTerminal(snap.tabs[0].id)}>
          &times;
        </button>
      ) : null}
    </div>
  );
}
