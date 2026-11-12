import { Bubble, Button, Chip, Icon, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { BlockCard } from "@/screens/permissions/BlockCard";
import { Markdown } from "./markdown";
import type { AttachmentChip, Item } from "./state";
import type { SummaryKind } from "./types";

export interface MessageActions {
  projectName: string;
  canSave: boolean;
  onPermission: (requestId: string, decision: "allow" | "deny") => void;
  onSaveMessage: (messageId: string) => void;
  onSaveAttachment: (attachmentId: string) => void;
}

function NoticeView({ item }: { item: Extract<Item, { kind: "notice" }> }) {
  const { t } = useI18n();
  const shell = useShell();
  return (
    <div className="chat-notice" role={item.tone === "bad" ? "alert" : "status"}>
      <Pill tone={item.tone === "bad" ? "bad" : item.tone === "warn" ? "warn" : "acc"}>{item.text}</Pill>
      {item.action === "open_engines" ? (
        <Button size="sm" variant="primary" onClick={() => shell.openSettings("engines")}>
          {t("chat.err.openEngines")}
        </Button>
      ) : null}
    </div>
  );
}

export function AttachmentView({ chip, onSave, saveLabel }: { chip: AttachmentChip; onSave?: () => void; saveLabel: string }) {
  const { t } = useI18n();
  return (
    <Chip className="chat-chip">
      {chip.kind === "image" && chip.previewUrl ? (
        <img className="chat-thumb" src={chip.previewUrl} alt="" />
      ) : (
        <Icon name={chip.kind === "image" ? "image" : chip.transcribed ? "mic" : "file"} size={14} strokeWidth={1.8} />
      )}
      <span className="chat-chip-name">{chip.name}</span>
      {chip.transcribed ? <Pill tone="ok">{t("chat.voice.transcribed")}</Pill> : null}
      {onSave && chip.id ? (
        <button type="button" className="chat-link" onClick={onSave}>
          {saveLabel}
        </button>
      ) : null}
    </Chip>
  );
}

function ToolLine({ item }: { item: Extract<Item, { kind: "tool" }> }) {
  const { t } = useI18n();
  const kind: SummaryKind = item.summary.kind;
  const verb = kind === "other" ? t("chat.tool.other", { tool: item.name }) : t(`chat.tool.${kind}`);
  const state = item.result ? (item.result.isError ? "failed" : "done") : "running";
  return (
    <div className="chat-tool fade" data-testid="tool-use">
      <Chip className="chat-tool-chip">
        <Icon name="terminal" size={14} strokeWidth={1.8} />
        <span>{verb}</span>
        {item.summary.target ? <span className="mono chat-tool-target">{item.summary.target}</span> : null}
        <Pill tone={state === "done" ? "ok" : state === "failed" ? "bad" : "default"}>{t(`chat.tool.${state}`)}</Pill>
      </Chip>
      {!item.result && item.output ? (
        <pre className="mono chat-tool-live" aria-live="polite" aria-label={t("chat.tool.live")} data-testid="tool-live">{item.output}</pre>
      ) : null}
      {item.result?.text ? (
        <details className="chat-tool-result">
          <summary>{t("chat.tool.result")}</summary>
          <pre className="mono">{item.result.text}</pre>
        </details>
      ) : null}
    </div>
  );
}

function PermissionCard({ item, actions }: { item: Extract<Item, { kind: "permission" }>; actions: MessageActions }) {
  const { t } = useI18n();
  const { summary, tool, request_id: requestId } = item.body;
  const kind = summary.kind;
  const params = { tool, target: summary.target, project: actions.projectName };
  if (item.resolution) {
    const { decision, by } = item.resolution;
    const label = decision === "allow" ? t("chat.perm.allowed") : by === "timeout" ? t("chat.perm.timeout") : by === "stop" ? t("chat.perm.stopped") : t("chat.perm.denied");
    return (
      <Bubble from="assistant" className="chat-perm-done" data-testid="permission-done">
        <Pill tone={decision === "allow" ? "ok" : "bad"}>{label}</Pill>
        <span className="p">{t(`chat.perm.title.${kind}`, params)}</span>
        {summary.target ? <span className="mono chat-tool-target">{summary.target}</span> : null}
      </Bubble>
    );
  }
  return (
    <Bubble from="assistant" className="chat-perm" role="group" aria-label={t("chat.perm.badge")} data-testid="permission-card">
      <div className="chat-perm-head">
        <Pill tone="warn">{t("chat.perm.badge")}</Pill>
        <span className="h2">{t(`chat.perm.title.${kind}`, params)}</span>
      </div>
      <span className="p">{t(`chat.perm.text.${kind}`, params)}</span>
      <span className="p chat-small">{t("chat.perm.expires")}</span>
      <div className="chat-perm-actions">
        <Button variant="primary" size="sm" disabled={item.sending} onClick={() => actions.onPermission(requestId, "allow")}>
          {t("chat.perm.approve")}
        </Button>
        <Button variant="ghost" size="sm" disabled={item.sending} onClick={() => actions.onPermission(requestId, "deny")}>
          {t("chat.perm.reject")}
        </Button>
      </div>
    </Bubble>
  );
}

export function MessageItem({ item, actions }: { item: Item; actions: MessageActions }) {
  const { t } = useI18n();
  switch (item.kind) {
    case "user":
      return (
        <Bubble from="user" data-testid="user-message">
          {item.attachments.map((chip) => (
            <div key={chip.key} className="chat-chip-row">
              <AttachmentView chip={chip} saveLabel={t("chat.save")} onSave={actions.canSave && chip.id ? () => actions.onSaveAttachment(chip.id!) : undefined} />
            </div>
          ))}
          <span className="chat-pre">{item.text}</span>
        </Bubble>
      );
    case "assistant":
      return (
        <Bubble from="assistant" data-testid="assistant-message">
          <Markdown text={item.text} />
          {item.streaming ? <span className="caret" aria-hidden="true" /> : null}
          {item.status !== "complete" ? (
            <div className="chat-bubble-foot">
              <Pill tone={item.status === "error" ? "bad" : "warn"}>{item.status === "error" ? t("chat.failed") : t("chat.stopped")}</Pill>
            </div>
          ) : null}
          {!item.streaming && item.messageId && actions.canSave && item.text ? (
            <div className="chat-bubble-foot">
              <button type="button" className="chat-link" onClick={() => actions.onSaveMessage(item.messageId!)}>
                {t("chat.save")}
              </button>
            </div>
          ) : null}
        </Bubble>
      );
    case "tool":
      return <ToolLine item={item} />;
    case "permission":
      return <PermissionCard item={item} actions={actions} />;
    case "block":
      return <BlockCard block={item.block} />;
    case "notice":
      return <NoticeView item={item} />;
  }
}
