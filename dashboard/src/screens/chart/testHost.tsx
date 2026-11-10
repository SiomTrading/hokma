/**
 * Test host: the part of the shell that mounts screens, without the rest of the shell. Every tab is mounted under its
 * own key (like `Shell.tsx` does) and the background ones stay mounted (the chart is keepAlive). Seed `hokma.tabs.v1`
 * (sessionStorage) with `seedTabs` before mounting to start from tabs that "were open" in the previous page.
 */
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { ShellProvider } from "@/shell/store";
import { useShell } from "@/shell/useShell";
import type { ShellApi } from "@/shell/store";
import { ChartScreen } from "./ChartScreen";

export const TABS_KEY = "hokma.tabs.v1";

/** The tabs of this browser tab as a previous page left them (sessionStorage, one per browser tab). */
export function seedTabs(tabs: Array<{ key: string; params: Record<string, string> }>, activeKey?: string): void {
  sessionStorage.setItem(
    TABS_KEY,
    JSON.stringify({
      tabs: tabs.map((tab) => ({ key: tab.key, screen: "chart", params: tab.params })),
      activeKey: activeKey ?? tabs[tabs.length - 1]?.key ?? null,
    }),
  );
}

/** What the shell is doing, in the page: open tabs and which one is active. */
function Tabs() {
  const shell = useShell();
  return (
    <>
      <output aria-label="tab-keys">{shell.tabs.map((tab) => tab.key).join("|")}</output>
      <output aria-label="active-key">{shell.activeKey ?? ""}</output>
      {shell.tabs
        .filter((tab) => tab.screen === "chart")
        .map((tab) => (
          <div key={tab.key} hidden={tab.key !== shell.activeKey} data-tab={tab.key} className="hk">
            <ChartScreen tabKey={tab.key} params={tab.params} active={tab.key === shell.activeKey} />
          </div>
        ))}
    </>
  );
}

let latest: ShellApi | null = null;
function Grab() {
  latest = useShell();
  return null;
}

/** The shell API of the page mounted last, to open and close tabs from a test as other screens do. */
export function shellApi(): ShellApi {
  if (!latest) throw new Error("no host mounted");
  return latest;
}

/** Mounts the host, as a page load would. `unmount()` and `mountHost()` again make an F5. */
export function mountHost(extra?: ReactNode) {
  return render(
    <ShellProvider>
      <Grab />
      <Tabs />
      {extra}
    </ShellProvider>,
  );
}
