import { useEffect, useSyncExternalStore } from "react";
import { Dot, Icon, IconButton, cx } from "@/design";
import { useI18n } from "@/i18n";
import { liveCounts } from "@/screens/chat/activity";
import { getHub } from "@/screens/chat/hub";
import { useActivity } from "@/screens/chat/useActivity";
import { BackgroundFeed, useBackground } from "@/screens/panel/feed";
import { getPanelSections } from "./registry";
import type { PanelSectionRegistration } from "./registry";
import { Boundary } from "./Slot";
import { ackBlock, ackFailure, setCollapsed, usePanelState } from "./panelState";
import { useShell } from "./useShell";

function Summary({ use }: { use: () => string }) {
  return <>{use()}</>;
}

function Section({ section }: { section: PanelSectionRegistration }) {
  const { t } = useI18n();
  const { collapsed } = usePanelState();
  const folded = collapsed[section.id] === true;
  const Body = section.component;
  const Actions = section.actions;
  return (
    <section className={cx("pn-sec", section.fill && "fill")} data-section={section.id} aria-label={t(section.titleKey)}>
      <div className="pn-head">
        <button type="button" className="pn-toggle" aria-expanded={!folded} onClick={() => setCollapsed(section.id, !folded)}>
          <Icon name={folded ? "chevronRight" : "chevronDown"} size={13} />
          <span className="sec">{t(section.titleKey)}</span>
          {folded && section.useSummary ? (
            <span className="pn-sum hint">
              <Summary use={section.useSummary} />
            </span>
          ) : null}
        </button>
        {Actions ? (
          <div className="pn-actions">
            <Boundary fallback={null}>
              <Actions />
            </Boundary>
          </div>
        ) : null}
      </div>
      <div className="pn-body" hidden={folded}>
        <Boundary>
          <Body />
        </Boundary>
      </div>
    </section>
  );
}

function OpenPanel() {
  const { t } = useI18n();
  const shell = useShell();
  const sections = getPanelSections();
  const { followed } = usePanelState();
  const { failedTurn, blocked } = liveCounts(useActivity(followed?.conversationId ?? null));
  // Opening the panel is how a failure or a block is seen: the rail stops showing red or amber for it.
  useEffect(() => {
    if (failedTurn) ackFailure(failedTurn);
  }, [failedTurn]);
  useEffect(() => {
    if (blocked) ackBlock(blocked);
  }, [blocked]);
  return (
    <aside className="right" aria-label={t("rightPanel.title")}>
      <div className="pn-title">
        <span className="sec" style={{ padding: 0 }}>
          {t("rightPanel.title")}
        </span>
        <IconButton icon="chevronRight" size={15} label={t("rightPanel.collapse")} onClick={shell.toggleRightPanel} />
      </div>
      {sections.length === 0 ? (
        <p className="p" style={{ padding: "0 14px" }}>
          {t("rightPanel.unavailable")}
        </p>
      ) : (
        <>
          {sections.filter((s) => s.fill).map((s) => <Section key={s.id} section={s} />)}
          <div className="pn-rest">
            {sections.filter((s) => !s.fill).map((s) => <Section key={s.id} section={s} />)}
          </div>
        </>
      )}
    </aside>
  );
}

/** The 36 px strip of the minimized panel: a dot for the state and the two counters. Nothing stops while it is minimized. */
function Rail() {
  const { t } = useI18n();
  const shell = useShell();
  const { followed, acked, ackedBlock } = usePanelState();
  const conv = useActivity(followed?.conversationId ?? null);
  const { running } = useBackground();
  const notices = getHub().notices;
  const noticeCount = useSyncExternalStore(notices.subscribe, notices.count);
  const counts = { ...liveCounts(conv), waiting: liveCounts(conv).waiting || noticeCount > 0 };   // an order that waits for an answer is amber too (3390b)
  const background = running.length;
  const unseenBlock = counts.blocked !== null && counts.blocked !== ackedBlock;
  const tone = counts.waiting ? "waiting" : unseenBlock ? "blocked" : counts.agents > 0 ? "working" : counts.failedTurn && counts.failedTurn !== acked ? "error" : "idle";
  const parts: string[] = [];
  if (counts.agents > 0) parts.push(t(counts.agents === 1 ? "rightPanel.rail.agents.one" : "rightPanel.rail.agents", { n: counts.agents }));
  if (background > 0) parts.push(t(background === 1 ? "rightPanel.rail.background.one" : "rightPanel.rail.background", { n: background }));
  if (tone === "blocked") parts.unshift(t("rightPanel.rail.blocked"));
  if (counts.waiting) parts.unshift(t("rightPanel.rail.waiting"));
  if (tone === "error") parts.unshift(t("rightPanel.rail.error"));
  const label = parts.length > 0 ? `${parts.join(", ")}. ${t("rightPanel.rail.open")}` : t("rightPanel.rail.idle");
  return (
    <aside className="right rail" aria-label={t("rightPanel.rail.label")} data-tone={tone}>
      <button type="button" className="pn-rail" title={label} aria-label={label} data-tone={tone} data-testid="panel-rail" onClick={shell.toggleRightPanel}>
        <Dot className={cx("pn-dot", tone === "idle" ? "idle" : tone === "blocked" ? "waiting" : tone)} />
        {counts.agents > 0 ? <span className="pn-count" data-testid="rail-agents">{counts.agents}</span> : null}
        {background > 0 ? <span className="pn-count bg" data-testid="rail-background">{background}</span> : null}
      </button>
      <span className="sr-only" role="status" aria-live="polite">
        {label}
      </span>
    </aside>
  );
}

/** The column on the right, or its rail when minimized. One for the whole app: it follows the chat that has the focus. */
export function RightPanel() {
  const { layout } = useShell();
  const { collapsed } = usePanelState();
  return (
    <BackgroundFeed open={layout.rightPanel && collapsed.background !== true}>{layout.rightPanel ? <OpenPanel /> : <Rail />}</BackgroundFeed>
  );
}
