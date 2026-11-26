import { useEffect, useRef, useState } from "react";
import { Icon, Pill, cx } from "@/design";
import { useI18n } from "@/i18n";
import { blockTextKey } from "@/screens/permissions/BlockCard";
import { usePanelState } from "@/shell/panelState";
import { liveCounts, liveStatus } from "../chat/activity";
import type { AgentRow, ConvActivity, Step, TurnRow } from "../chat/activity";
import { Markdown } from "../chat/markdown";
import { useActivity } from "../chat/useActivity";
import { useAgentNamer } from "./agentNames";
import { formatElapsed, useNow } from "./clock";
import { StepLine, stepPhrase } from "./StepLine";
import "./panel.css";

const MAX_TURNS = 20;
const PROMPT_CHARS = 60;
const HIGHLIGHT_MS = 2000;

type Shown = "working" | "waiting" | "done" | "error" | "stopped";

/** What the folded section says and what the narrow rail counts: nothing for a conversation that is not open. */
export function useAgentsSummary(): string {
  const { t } = useI18n();
  const { followed } = usePanelState();
  const { agents } = liveCounts(useActivity(followed?.conversationId ?? null));
  return agents > 0 ? t("panel.agents.summary.busy", { n: agents }) : t("panel.agents.summary.idle");
}

/** The conversation the panel follows, named: "Mesa Futuros" for a desk chat. */
export function useFollowedLabel(): string {
  const { t } = useI18n();
  const { followed } = usePanelState();
  if (!followed) return "";
  return followed.source === "desk" ? t("rightPanel.deskConversation", { name: followed.label }) : followed.label;
}

interface RowProps {
  kind: "main" | "agent";
  id: string;
  depth: number;
  name: string;
  task: string;
  status: Shown;
  startedAt: number;
  endedAt: number | null;
  step: string;
  result: string | null;
  error: string;
  steps: Step[];
  turnRunning: boolean;
  failedTurn: boolean;
  blocks?: TurnRow["blocks"];
  highlight?: boolean;
  folded?: boolean;
  onFold?: () => void;
  now: number;
}

function Row(p: RowProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (p.highlight) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [p.highlight]);
  const active = p.status === "working" || p.status === "waiting";
  const clock = formatElapsed((active ? p.now : (p.endedAt ?? p.now)) - p.startedAt);
  const action =
    p.status === "error" || p.status === "stopped" ? t("panel.agents.viewReason") : p.kind === "main" ? t("panel.agents.viewSteps") : p.status === "done" ? t("panel.agents.viewResult") : "";
  const detail = (() => {
    if (p.status === "stopped") return t("panel.agents.stoppedMsg");
    if (p.status === "error") return p.error || p.result || t("chat.failed");
    return p.kind === "agent" ? p.result : null;
  })();
  return (
    <div
      ref={ref}
      className={cx("pn-row", p.highlight && "hl")}
      style={{ paddingLeft: 4 + p.depth * 12 }}
      data-testid={p.kind === "main" ? "turn-row" : "agent-row"}
      data-status={p.status}
      data-turn={p.kind === "main" ? p.id : undefined}
    >
      <span className={cx("pn-dot", p.status)} aria-hidden="true" />
      <div className="pn-main">
        <div className="pn-line">
          {p.onFold ? (
            <button type="button" className="pn-fold" aria-expanded={!p.folded} aria-label={t("panel.agents.fold")} onClick={p.onFold}>
              <Icon name={p.folded ? "chevronRight" : "chevronDown"} size={12} />
            </button>
          ) : null}
          <span className="pn-name">{p.name}</span>
          <span className="pn-clock mono">{clock}</span>
        </div>
        {p.task ? (
          <div className="pn-task hint" title={p.task}>
            {p.task}
          </div>
        ) : null}
        {p.blocks && p.blocks.length > 0 ? <div className="pn-step hint" data-testid="turn-blocked">{t(p.blocks.length === 1 ? "panel.agents.blocked.one" : "panel.agents.blocked", { n: p.blocks.length })}</div> : null}
        {active && (p.step || p.status === "waiting") ? <div className="pn-step hint">{p.status === "waiting" ? t("panel.agents.waiting") : p.step}</div> : null}
        {action ? (
          <button type="button" className="chat-link pn-act" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? t("panel.agents.hide") : action}
          </button>
        ) : null}
        {open ? (
          <div className="pn-detail">
            {detail !== null ? (
              p.status === "done" && p.kind === "agent" ? (
                detail ? <Markdown text={detail} /> : <p className="hint">{t("panel.agents.noResult")}</p>
              ) : (
                <p className="p">{detail}</p>
              )
            ) : null}
            {p.blocks?.map((block) => (
              <div key={block.id} className="pn-block" data-testid="block-line">
                <Pill tone="bad">{t(`block.title.${blockTextKey(block)}`)}</Pill>
                {block.target ? <span className="mono hint pn-task" title={block.target}>{block.target}</span> : null}
              </div>
            ))}
            {p.steps.length > 0 ? (
              <details className="pn-steps">
                <summary>{t("panel.agents.steps", { n: p.steps.length })}</summary>
                {p.steps.map((step) => (
                  <StepLine key={step.id} step={step} running={p.turnRunning} failedTurn={p.failedTurn} />
                ))}
              </details>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function AgentTree({ conv, turn, parent, depth, now, name }: { conv: ConvActivity; turn: TurnRow; parent: string; depth: number; now: number; name: (slug: string) => string }) {
  const { t } = useI18n();
  const children = turn.agents.filter((agent) => agent.parent === parent);
  return (
    <>
      {children.map((agent: AgentRow) => {
        const status = liveStatus(conv, agent.status, agent.id);
        const current = agent.steps.find((step) => step.id === agent.step) ?? agent.steps[agent.steps.length - 1];
        return (
          <div key={agent.id}>
            <Row
              kind="agent" id={agent.id} depth={depth} name={name(agent.slug)} task={agent.description} status={status}
              startedAt={agent.startedAt} endedAt={agent.endedAt} step={current ? stepPhrase(t, current) : ""} result={agent.result}
              error="" steps={agent.steps} turnRunning={agent.status === "working"} failedTurn={agent.status !== "done"} now={now}
            />
            <AgentTree conv={conv} turn={turn} parent={agent.id} depth={depth + 1} now={now} name={name} />
          </div>
        );
      })}
    </>
  );
}

export function AgentsSection() {
  const { t } = useI18n();
  const { followed, focus } = usePanelState();
  const conv = useActivity(followed?.conversationId ?? null);
  const namer = useAgentNamer();
  const label = useFollowedLabel();
  const [foldedByUser, setFolded] = useState<Record<string, boolean>>({});
  const [all, setAll] = useState(false);
  const [lit, setLit] = useState<string | null>(null);
  const counts = liveCounts(conv);
  const now = useNow(counts.agents > 0 || counts.shells > 0);

  useEffect(() => {
    if (!focus) return;
    setLit(focus.turnId);
    const timer = setTimeout(() => setLit(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [focus]);

  if (!followed) return <p className="hint pn-pad">{t("panel.agents.none")}</p>;
  const turns = [...conv.turns].reverse();
  const shown = all ? turns : turns.slice(0, MAX_TURNS);
  const mainName = conv.agent ? namer(conv.agent) : t("panel.agents.main");
  return (
    <div className="pn-list">
      <span className="sr-only" role="status">
        {t(counts.agents > 0 ? "panel.agents.summary.busy" : "panel.agents.summary.idle", { n: counts.agents })}
      </span>
      {label ? <p className="hint pn-pad pn-conv">{label}</p> : null}
      {turns.length === 0 ? <p className="hint pn-pad">{t("panel.agents.empty")}</p> : null}
      {shown.map((turn, index) => {
        const waitingMain = turn.status === "working" && conv.pending.some((p) => p.agentId === "");
        const status: Shown = waitingMain ? "waiting" : turn.status;
        const defaultFolded = index > 1 && turn.status !== "working";
        const folded = foldedByUser[turn.id] ?? defaultFolded;
        const lastStep = turn.steps[turn.steps.length - 1];
        const written = turn.origin ? t("chat.origin.agents") : turn.prompt;
        const prompt = written.length > PROMPT_CHARS ? `${written.slice(0, PROMPT_CHARS)}...` : written;
        const hasKids = turn.agents.length > 0;
        return (
          <div key={turn.id} className="pn-turn">
            <Row
              kind="main" id={turn.id} depth={0} name={mainName} task={prompt} status={status} startedAt={turn.startedAt} endedAt={turn.endedAt}
              step={lastStep ? stepPhrase(t, lastStep) : ""} result={null} error={turn.error} steps={turn.steps} blocks={turn.blocks}
              turnRunning={turn.status === "working"} failedTurn={turn.status !== "done"} highlight={lit === turn.id} now={now}
              folded={folded} onFold={hasKids ? () => setFolded((m) => ({ ...m, [turn.id]: !folded })) : undefined}
            />
            {!folded ? <AgentTree conv={conv} turn={turn} parent="" depth={1} now={now} name={namer} /> : null}
          </div>
        );
      })}
      {!all && turns.length > MAX_TURNS ? (
        <button type="button" className="chat-link pn-pad" onClick={() => setAll(true)}>
          {t("panel.agents.more", { n: turns.length - MAX_TURNS })}
        </button>
      ) : null}
    </div>
  );
}
