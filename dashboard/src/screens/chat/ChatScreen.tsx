import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState, useSyncExternalStore } from "react";
import type { DragEvent } from "react";
import { Button, EmptyState, ErrorState, Isotype, Loading, Modal, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell, useShellEvent } from "@/shell/useShell";
import type { ScreenProps } from "@/shell/registry";
import "./chat.css";
import { USAGE_WARN_PCT, chatOptions, errorText, saveToProject, weeklyUsagePct } from "./chatApi";
import { parseCommand } from "./commands";
import { Composer } from "./Composer";
import type { ComposerHandle } from "./Composer";
import { getHub } from "./hub";
import type { HubClient } from "./hub";
import { MessageItem } from "./Messages";
import { initialState, reducer } from "./state";
import type { AttachmentChip } from "./state";
import type { ChatOptions, ModelId, ServerEvent } from "./types";

function useHubStatus() {
  const hub = getHub();
  return useSyncExternalStore(
    (listener) => hub.onStatus(listener),
    () => hub.status,
  );
}

/** Whether the weekly-cap warning has been given for the current crossing (shared by every chat tab). */
const usageWarning = { given: false };

/** Tests: forget a warning given earlier. */
export function resetUsageWarning(): void {
  usageWarning.given = false;
}

const USAGE_REFRESH_MS = 30_000;

export function ChatScreen({ tabKey, params, active }: ScreenProps) {
  const { t } = useI18n();
  const shell = useShell();
  const hub = getHub();
  const status = useHubStatus();
  const fixedConversation = params.conversation ?? null;
  const [state, dispatch] = useReducer(reducer, fixedConversation, initialState);
  const [options, setOptions] = useState<ChatOptions | null>(null);
  const [optionsTick, setOptionsTick] = useState(0);
  const [usagePct, setUsagePct] = useState<number | null>(null);
  const [confirmOpus, setConfirmOpus] = useState(false);
  const [modelMenuRequest, setModelMenuRequest] = useState(0);
  const [dragging, setDragging] = useState(false);
  const composer = useRef<ComposerHandle>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const lastCommand = useRef<{ name: string; arg: string | null } | null>(null);
  const tRef = useRef(t);
  tRef.current = t;

  const client = useRef<HubClient | null>(null);
  if (client.current === null) {
    client.current = { id: fixedConversation, awaiting: false, onEvent: () => undefined, onOpen: () => undefined, claim: () => undefined };
  }

  const loadUsage = useCallback(() => {
    weeklyUsagePct().then(setUsagePct, () => undefined);
  }, []);
  const loadUsageRef = useRef(loadUsage);
  loadUsageRef.current = loadUsage;

  const handleEvent = useCallback((event: ServerEvent) => {
    const t = tRef.current;
    const self = client.current!;
    dispatch({ type: "event", event, errorText });
    if (event.type === "done" || event.type === "error") self.awaiting = false;
    if (event.type === "done") loadUsageRef.current();
    if (event.type === "command_result") {
      const command = lastCommand.current;
      lastCommand.current = null;
      const read = !command || command.arg === null;
      const push = (text: string) => dispatch({ type: "notice", tone: "info", text });
      if (event.name === "model") {
        if (read && event.options) setModelMenuRequest((n) => n + 1);
        else if (typeof event.value === "string") push(t("chat.cmd.modelSet", { name: t(`chat.model.${event.value}`) }));
      } else if (event.name === "agent") {
        if (read) push(typeof event.value === "string" ? t("chat.cmd.currentAgent", { name: event.value }) : t("chat.cmd.noAgent"));
        else push(typeof event.value === "string" ? t("chat.cmd.agentSet", { name: event.value }) : t("chat.cmd.agentCleared"));
      } else if (event.name === "project") {
        const project = event.value as { name?: string } | null;
        if (project?.name) push(t(read ? "chat.cmd.currentProject" : "chat.cmd.projectSet", { name: project.name }));
      }
    }
  }, []);

  useEffect(() => {
    const self = client.current!;
    self.onEvent = handleEvent;
    self.onOpen = () => {
      if (self.id) hub.send({ type: "open", conversation_id: self.id });
    };
    self.claim = (id) => {
      self.id = id;
      self.awaiting = false;
    };
    return hub.register(self);
  }, [hub, handleEvent]);

  useEffect(() => {
    let alive = true;
    chatOptions().then(
      (data) => alive && setOptions(data),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [state.agent, state.project?.id, optionsTick]);

  // The agent list changes in Settings: the "/" menu has to follow it.
  useShellEvent("agents:changed", () => setOptionsTick((n) => n + 1));

  // Weekly cap: read while this tab is in front, and again when the settings or a turn change it.
  useEffect(() => {
    if (!active) return;
    loadUsage();
    const timer = setInterval(loadUsage, USAGE_REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, loadUsage]);
  useShellEvent("usage:changed", loadUsage);
  useShellEvent("settings:closed", loadUsage);

  // Crossing the warning line gives one notice in the chat; going back under it re-arms the warning.
  useEffect(() => {
    if (usagePct === null || !active) return;
    if (usagePct >= USAGE_WARN_PCT) {
      if (usageWarning.given) return;
      usageWarning.given = true;
      dispatch({ type: "notice", tone: "warn", text: tRef.current("chat.usage.warn", { pct: Math.round(usagePct) }) });
    } else {
      usageWarning.given = false;
    }
  }, [usagePct, active]);

  const { setTabTitle } = shell;
  useEffect(() => {
    setTabTitle(tabKey, state.title || undefined);
  }, [setTabTitle, tabKey, state.title]);

  // Ctrl N: the plain chat tab starts over. A tab bound to a stored conversation keeps it.
  useShellEvent("conversation:new", () => {
    if (fixedConversation) return;
    client.current!.id = null;
    client.current!.awaiting = false;
    lastCommand.current = null;
    dispatch({ type: "reset", id: null });
    composer.current?.focus();
  });

  useEffect(() => {
    if (active) composer.current?.focus();
  }, [active]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    const last = state.items[state.items.length - 1];
    if (nearBottom || last?.kind === "user") el.scrollTop = el.scrollHeight;
  }, [state.items, state.loading]);

  const send = (text: string, attachments: AttachmentChip[]): boolean => {
    const self = client.current!;
    const current = stateRef.current;
    const command = attachments.length === 0 ? parseCommand(text) : null;
    if (command?.name === "model" && command.arg?.toLowerCase() === "opus" && current.model !== "opus") {
      setConfirmOpus(true); // nothing changes until the owner confirms
      return true;
    }
    const message: Record<string, unknown> = { type: "send", conversation_id: self.id, text, attachments: attachments.map((chip) => chip.id) };
    if (self.id === null) message.model = current.model;
    if (!hub.send(message)) {
      shell.toast({ tone: "warn", text: t("chat.composer.notConnected") });
      return false;
    }
    if (self.id === null) self.awaiting = true;
    if (command) lastCommand.current = command;
    else dispatch({ type: "local_send", text, attachments });
    return true;
  };

  const pickModel = (model: ModelId) => {
    dispatch({ type: "local_model", model });
    const self = client.current!;
    if (self.id) {
      lastCommand.current = { name: "model", arg: model };
      hub.send({ type: "command", conversation_id: self.id, name: "model", arg: model });
    }
  };

  // Opus burns the weekly cap several times faster: the model only changes after the owner confirms.
  const requestModel = (model: ModelId) => {
    if (model === "opus" && stateRef.current.model !== "opus") setConfirmOpus(true);
    else pickModel(model);
  };

  const stop = () => {
    const self = client.current!;
    if (!self.id) return;
    dispatch({ type: "local_stop" });
    hub.send({ type: "stop", conversation_id: self.id });
  };

  const answerPermission = (requestId: string, decision: "allow" | "deny") => {
    const self = client.current!;
    if (!self.id) return;
    dispatch({ type: "permission_sending", requestId });
    hub.send({ type: "permission_response", conversation_id: self.id, request_id: requestId, decision });
  };

  const save = (target: { message_id: string } | { attachment_id: string }) => {
    const id = client.current!.id;
    if (!id) return;
    saveToProject(id, target).then(
      (done) => shell.toast({ tone: "ok", text: t("chat.save.done", { project: done.project.name, path: done.path }) }),
      () => shell.toast({ tone: "bad", text: t("chat.save.failed") }),
    );
  };

  const retryOpen = () => {
    dispatch({ type: "reset", id: client.current!.id });
    client.current!.onOpen();
  };

  const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer?.types ?? []).includes("Files");
  const projectName = state.project?.name ?? options?.default_project?.name ?? "";

  return (
    <section
      className="pane chat-root"
      aria-label={t("chat.header.label")}
      onDragEnter={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (hasFiles(event)) event.preventDefault();
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        setDragging(false);
        composer.current?.addFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <Isotype mono size={340} className="chat-watermark" />
      <header className="chat-head">
        <span className="lbl">{t("chat.project")}</span>
        {projectName ? <Pill tone="acc">{projectName}</Pill> : null}
        {state.agent ? <Pill>{t("chat.agentValue", { name: state.agent })}</Pill> : null}
        {status !== "open" ? (
          <Pill tone="warn" role="status" style={{ marginLeft: "auto" }}>
            <span className="dot live" aria-hidden="true" />
            {t("chat.reconnecting")}
          </Pill>
        ) : null}
      </header>

      <div ref={scroller} className="chat-scroll" role="log" aria-live="polite" aria-label={t("chat.messages")}>
        {state.loading ? <Loading label={t("chat.loading")} /> : null}
        {state.loadError ? <ErrorState message={state.loadError} onRetry={retryOpen} /> : null}
        {!state.loading && !state.loadError && state.items.length === 0 ? (
          <EmptyState icon="chat" title={t("chat.empty.title")} description={t("chat.empty.text")} className="chat-empty" />
        ) : null}
        {state.items.map((item) => (
          <MessageItem
            key={item.key}
            item={item}
            actions={{
              projectName,
              canSave: state.id !== null,
              onPermission: answerPermission,
              onSaveMessage: (id) => save({ message_id: id }),
              onSaveAttachment: (id) => save({ attachment_id: id }),
            }}
          />
        ))}
      </div>

      <Composer
        ref={composer}
        model={state.model}
        running={state.running}
        stopping={state.stopping}
        options={options}
        modelMenuRequest={modelMenuRequest}
        usagePct={usagePct}
        onModel={requestModel}
        onSend={send}
        onStop={stop}
      />

      {confirmOpus ? (
        <Modal label={t("chat.opus.title")} onClose={() => setConfirmOpus(false)} style={{ width: "min(420px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
          <h2 className="h1">{t("chat.opus.title")}</h2>
          <p className="p">{t("chat.opus.text")}</p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setConfirmOpus(false)}>
              {t("chat.opus.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setConfirmOpus(false);
                pickModel("opus");
              }}
            >
              {t("chat.opus.confirm")}
            </Button>
          </div>
        </Modal>
      ) : null}

      {dragging ? (
        <div className="chat-drop" aria-hidden="true">
          <span className="h2">{t("chat.composer.drop")}</span>
        </div>
      ) : null}
    </section>
  );
}
