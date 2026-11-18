import { useState } from "react";
import type { ComponentType } from "react";
import { useShell } from "@/shell/useShell";
import { DeskBar } from "./DeskBar";
import { AgentsTab } from "./tabs/AgentsTab";
import { ConfigTab } from "./tabs/ConfigTab";
import { OperationsTab } from "./tabs/OperationsTab";
import { RulesTab } from "./tabs/RulesTab";
import { TerminalTab } from "./tabs/TerminalTab";
import type { TabProps } from "./tabs/types";
import { isDeskTab } from "./types";
import type { DeskSummary, DeskTab } from "./types";

const TAB_COMPONENT: Record<DeskTab, ComponentType<TabProps>> = {
  terminal: TerminalTab,
  agents: AgentsTab,
  operations: OperationsTab,
  rules: RulesTab,
  config: ConfigTab,
};

interface Props {
  desk: DeskSummary;
  desks: DeskSummary[];
  tabKey: string;
  /** Tab named in the screen params, if any. It only picks where the desk opens. */
  initialTab?: string;
}

/** One desk: the top bar and the body of the tab that is open. Switching tabs is local. */
export function DeskScreen({ desk, desks, tabKey, initialTab }: Props) {
  const shell = useShell();
  const [picked, setPicked] = useState<DeskTab | null>(isDeskTab(initialTab) ? initialTab : null);
  const tab: DeskTab = picked ?? "terminal";
  const Body = TAB_COMPONENT[tab];
  return (
    <section className="desks-screen" aria-label={desk.name}>
      <DeskBar
        desk={desk}
        desks={desks}
        tab={tab}
        tabKey={tabKey}
        onTab={setPicked}
        onPickDesk={(id) => shell.openScreen("desks", { desk: String(id) })}
      />
      <div className="desks-body" role="tabpanel" id={`${tabKey}-panel`} aria-labelledby={`${tabKey}-tab-${tab}`}>
        <Body desk={desk} />
      </div>
    </section>
  );
}
