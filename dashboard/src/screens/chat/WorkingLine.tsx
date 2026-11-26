import { cx } from "@/design";
import { useI18n } from "@/i18n";
import { focusTurn, setCollapsed } from "@/shell/panelState";
import { useShell } from "@/shell/useShell";
import { useAgentNamer } from "../panel/agentNames";
import { formatElapsed, useNow } from "../panel/clock";
import "../panel/panel.css";
import { useActivity } from "./useActivity";

/**
 * The only trace of the work in the chat: one live line under the last message, "Iris esta trabajando, miralo en el panel".
 * It is not a message: it goes with the turn and is not in the history. Nothing of the tools or the steps shows here.
 */
export function WorkingLine({ conversationId, running }: { conversationId: string | null; running: boolean }) {
  const { t } = useI18n();
  const shell = useShell();
  const conv = useActivity(conversationId);
  const namer = useAgentNamer();
  const turn = [...conv.turns].reverse().find((candidate) => candidate.status === "working");
  const now = useNow(running);
  if (!running) return null;
  const working = turn?.agents.filter((agent) => agent.status === "working") ?? [];
  const asking = conv.pending[0];
  const askingAgent = asking?.agentId ? turn?.agents.find((agent) => agent.id === asking.agentId) : undefined;
  const mainName = conv.agent ? namer(conv.agent) : t("panel.agents.main");
  let text: string;
  if (asking) text = t("chat.working.waiting", { name: askingAgent ? namer(askingAgent.slug) : mainName });
  else if (working.length === 0) text = t("chat.working.main");
  else if (working.length === 1) text = t("chat.working.one", { name: namer(working[0]!.slug) });
  else text = t("chat.working.many", { n: working.length });
  const point = () => {
    if (!shell.layout.rightPanel) shell.toggleRightPanel();
    setCollapsed("agents", false);
    if (turn) focusTurn(turn.id);
  };
  const status = asking ? "waiting" : "working";
  return (
    <div className="chat-working fade" role="status" data-testid="working-line">
      <span className={cx("pn-dot", status)} aria-hidden="true" />
      <span>
        {text}
        {asking ? null : (
          <>
            {", "}
            <button type="button" className="chat-link" onClick={point}>
              {t("chat.working.link")}
            </button>
          </>
        )}
      </span>
      {turn ? <span className="pn-clock mono">{formatElapsed(now - turn.startedAt)}</span> : null}
    </div>
  );
}
