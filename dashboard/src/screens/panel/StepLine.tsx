import { Chip, Icon, Pill } from "@/design";
import { useI18n } from "@/i18n";
import "../chat/chat.css";
import type { Step } from "../chat/activity";

/** What a step is doing, as one phrase: "Leyendo data/prices.csv". The same words the chat used before the panel. */
export function stepPhrase(t: (key: string, params?: Record<string, string | number>) => string, step: Step): string {
  const verb = step.kind === "other" ? t("chat.tool.other", { tool: step.name }) : t(`chat.tool.${step.kind}`);
  return step.target ? `${verb} ${step.target}` : verb;
}

/**
 * A step of an agent in the panel: its verb and target, its state, the live output of a command that runs and the result
 * on demand. `running` is the turn still going; `failedTurn` says how to read a step that never got a result.
 */
export function StepLine({ step, running, failedTurn }: { step: Step; running: boolean; failedTurn: boolean }) {
  const { t } = useI18n();
  const verb = step.kind === "other" ? t("chat.tool.other", { tool: step.name }) : t(`chat.tool.${step.kind}`);
  const state = step.isError === true ? "failed" : step.isError === false ? "done" : running ? "running" : failedTurn ? "failed" : "done";
  return (
    <div className="chat-tool fade" data-testid="tool-use">
      <Chip className="chat-tool-chip">
        <Icon name="terminal" size={14} strokeWidth={1.8} />
        <span>{verb}</span>
        {step.target ? <span className="mono chat-tool-target">{step.target}</span> : null}
        <Pill tone={state === "done" ? "ok" : state === "failed" ? "bad" : "default"}>{t(`chat.tool.${state}`)}</Pill>
      </Chip>
      {step.result === null && step.output ? (
        <details className="chat-tool-result" data-testid="tool-output">
          <summary>{t("panel.step.output")}</summary>
          <pre className="mono chat-tool-live" aria-live="polite" aria-label={t("chat.tool.live")} data-testid="tool-live">
            {step.output}
          </pre>
        </details>
      ) : null}
      {step.result ? (
        <details className="chat-tool-result">
          <summary>{t("chat.tool.result")}</summary>
          <pre className="mono">{step.result}</pre>
        </details>
      ) : null}
    </div>
  );
}
