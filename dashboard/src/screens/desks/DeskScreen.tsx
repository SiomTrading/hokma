import { useCallback, useEffect, useRef, useState } from "react";
import type { ComponentType } from "react";
import { useShell } from "@/shell/useShell";
import { useI18n } from "@/i18n";
import { analysisApi } from "./analysisApi";
import { watchAnalysis } from "./analysisFeed";
import { failText } from "./configApi";
import { DeskBar } from "./DeskBar";
import { isFixedPanel, panelsOf, sameOrder } from "./panels";
import type { DeskTab } from "./panels";
import { deskPanelsApi, readPanels } from "./panelsApi";
import { refreshDesks } from "./store";
import { AgentsTab } from "./tabs/AgentsTab";
import { CalendarTab } from "./tabs/CalendarTab";
import { AnalysisTab } from "./tabs/AnalysisTab";
import { CompanyTab } from "./tabs/CompanyTab";
import { ConfigTab } from "./tabs/ConfigTab";
import { OperationsTab } from "./tabs/OperationsTab";
import { OptionDetailTab } from "./tabs/OptionDetailTab";
import { RulesTab } from "./tabs/RulesTab";
import { StrategiesTab } from "./tabs/StrategiesTab";
import { TerminalTab } from "./tabs/TerminalTab";
import type { TabProps } from "./tabs/types";
import { isDeskTab } from "./types";
import type { DeskSummary } from "./types";

const TAB_COMPONENT: Record<DeskTab, ComponentType<TabProps>> = {
  terminal: TerminalTab,
  agents: AgentsTab,
  operations: OperationsTab,
  rules: RulesTab,
  config: ConfigTab,
  company: CompanyTab,
  calendar: CalendarTab,
  analysis: AnalysisTab,
  strategies: StrategiesTab,
  option_detail: OptionDetailTab,
};

interface Props {
  desk: DeskSummary;
  desks: DeskSummary[];
  tabKey: string;
  /** Tab named in the screen params, if any. It only picks where the desk opens. */
  initialTab?: string;
}

/**
 * One desk: the top bar and the body of the tab that is open. Switching tabs is local. The panels and their order are the desk's
 * (SPEC-025, delivery 3): a move shows at once and is saved after it; what the chief adds or takes off by chat arrives with the desk.
 */
export function DeskScreen({ desk, desks, tabKey, initialTab }: Props) {
  const shell = useShell();
  const { t } = useI18n();
  const server = panelsOf(desk);
  const [order, setOrder] = useState<DeskTab[]>(server);
  /** The asset a click on a ticker of another panel asked Company to show. */
  const [companyFocus, setCompanyFocus] = useState<string | null>(null);
  /** Why a panel cannot be added to this desk (the Analysis panel on a desk that is not a stock options desk), for the "+ Add" menu. */
  const [blocked, setBlocked] = useState<Partial<Record<DeskTab, string>>>({});
  const [picked, setPicked] = useState<DeskTab | null>(isDeskTab(initialTab) ? initialTab : null);
  /** Saves in flight: while there is one, what the server sent is older than what the screen shows. */
  const saving = useRef(0);
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const serverKey = server.join(",");

  useEffect(() => {
    if (saving.current === 0) setOrder((now) => (sameOrder(now, server) ? now : server));
    // `serverKey` stands for the list the server sent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey, desk.id]);

  const tab: DeskTab = picked && order.includes(picked) ? picked : order.includes("terminal") ? "terminal" : (order[0] ?? "terminal");
  const Body = TAB_COMPONENT[tab];

  /** Runs the saves one after the other, so two quick moves reach the server in the order they were made. */
  const queue = useCallback(
    (work: () => Promise<unknown>, revertTo: DeskTab[], failure: string) => {
      saving.current += 1;
      const run = chain.current.then(work).catch((cause) => {
        setOrder(revertTo);
        shell.toast({ tone: "bad", text: failText(cause, failure) });
      });
      chain.current = run.finally(() => {
        saving.current -= 1;
        if (saving.current === 0) void refreshDesks();
      });
    },
    [shell],
  );

  const reorder = (next: DeskTab[]) => {
    const before = order;
    setOrder(next);
    queue(() => deskPanelsApi.set(desk.id, next), before, t("desks.panels.saveFailed"));
  };

  const add = (panel: DeskTab) => {
    const before = order;
    queue(
      async () => {
        const saved = readPanels(await deskPanelsApi.add(desk.id, panel));
        if (saved) setOrder(saved);
        setPicked(panel);
      },
      before,
      t("desks.panels.addFailed"),
    );
  };

  /** A ticker of another panel was clicked: Company opens with it, and is added first when the desk does not have it. */
  const openCompany = (symbol: string) => {
    setCompanyFocus(symbol);
    if (order.includes("company")) setPicked("company");
    else add("company");
  };

  const remove = (panel: DeskTab) => {
    if (isFixedPanel(panel)) return;
    const before = order;
    setOrder(order.filter((id) => id !== panel));
    queue(() => deskPanelsApi.remove(desk.id, panel), before, t("desks.panels.removeFailed"));
  };

  const hasAnalysis = order.includes("analysis");
  useEffect(() => {
    if (hasAnalysis) return;
    let alive = true;
    setBlocked({});
    analysisApi.list(desk.id, 1).then(
      (body) => {
        if (alive && !body.available) setBlocked({ analysis: body.message ?? t("desks.analysis.unavailable.text") });
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [desk.id, hasAnalysis, t]);

  // A run the chief opened from the chat opens its panel (and adds it when it was not there). A run the panel opened finds it already open.
  const openRef = useRef({ order, tab, add, setPicked });
  openRef.current = { order, tab, add, setPicked };
  useEffect(
    () =>
      watchAnalysis(desk.id, (event) => {
        if (event.event !== "started") return;
        const now = openRef.current;
        if (now.order.includes("analysis")) {
          if (now.tab !== "analysis") now.setPicked("analysis");
        } else now.add("analysis");
      }),
    [desk.id],
  );

  return (
    <section className="desks-screen" aria-label={desk.name}>
      <DeskBar
        desk={desk}
        desks={desks}
        order={order}
        tab={tab}
        tabKey={tabKey}
        onTab={setPicked}
        onOrder={reorder}
        onAddPanel={add}
        onRemovePanel={remove}
        blocked={blocked}
        onPickDesk={(id) => shell.openScreen("desks", { desk: String(id) })}
      />
      <div className="desks-body" role="tabpanel" id={`${tabKey}-panel`} aria-labelledby={`${tabKey}-tab-${tab}`}>
        <Body desk={desk} onOpenCompany={openCompany} focusSymbol={companyFocus} onFocusUsed={() => setCompanyFocus(null)} openPanel={(panel) => (order.includes(panel) ? setPicked(panel) : add(panel))} />
      </div>
    </section>
  );
}
