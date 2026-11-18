import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState, useSyncExternalStore } from "react";
import { EmptyState, ErrorState, Input, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { OpusConfirm } from "../agents/OpusConfirm";
import { errorText } from "../chat/chatApi";
import { Composer } from "../chat/Composer";
import type { ComposerHandle } from "../chat/Composer";
import { getHub } from "../chat/hub";
import type { HubClient } from "../chat/hub";
import { MessageItem } from "../chat/Messages";
import { initialState, reducer } from "../chat/state";
import type { AttachmentChip } from "../chat/state";
import type { ModelId, ServerEvent } from "../chat/types";
import "../chat/chat.css";
import { deskAgentsApi, errorCode } from "./agentsApi";
import { failText } from "./configApi";
import { BUDGET_EXHAUSTED, deskChatApi } from "./deskChatApi";
import type { DeskConversation } from "./deskChatApi";
import { refreshDesks } from "./store";
import type { DeskSummary } from "./types";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "blocked" } | { state: "ready"; conversation: DeskConversation };

function useHubStatus() {
  const hub = getHub();
  return useSyncExternalStore(
    (listener) => hub.onStatus(listener),
    () => hub.status,
  );
}

/** Share of the weekly token budget of the desk already used, 0 to 100; null when the desk has no cap. */
function budgetPct(desk: DeskSummary): number | null {
  if (!desk.token_budget || desk.token_budget <= 0) return null;
  return Math.min(100, Math.round((desk.tokens_week / desk.token_budget) * 100));
}

/**
 * The conversation of the desk chief, in the narrow right column of the Terminal. It asks the server for the desk's
 * conversation and from there it is the same chat as the global one: same socket, same messages, same box. The model
 * selector shows and changes the model of the chief in this desk, never the global default.
 */
export function DeskChat({ desk }: { desk: DeskSummary }) {
  const { t } = useI18n();
  const [load, setLoad] = useState<Load>({ state: "loading" });

  const read = useCallback(() => {
    setLoad({ state: "loading" });
    deskChatApi.conversation(desk.id).then(
      (conversation) => setLoad({ state: "ready", conversation }),
      (cause) => setLoad(errorCode(cause) === BUDGET_EXHAUSTED ? { state: "blocked" } : { state: "error", message: failText(cause, t("desks.chat.error")) }),
    );
  }, [desk.id, t]);

  useEffect(read, [read]);

  return (
    <Pane className="dterm-chat" aria-label={t("desks.terminal.chat")}>
      {load.state === "ready" ? (
        <DeskChatBody key={load.conversation.id} desk={desk} conversation={load.conversation} />
      ) : (
        <>
          <div className="dterm-ph">
            <span className="dterm-tab on">{t("desks.terminal.chat")}</span>
          </div>
          <div className="dterm-chat-body">
            {load.state === "loading" ? <Loading label={t("desks.chat.loading")} /> : null}
            {load.state === "error" ? <ErrorState message={load.message} onRetry={read} /> : null}
            {load.state === "blocked" ? <EmptyState icon="chat" title={t("desks.chat.budget.exhausted.title")} description={t("desks.chat.budget.exhausted")} /> : null}
          </div>
        </>
      )}
    </Pane>
  );
}

function DeskChatBody({ desk, conversation }: { desk: DeskSummary; conversation: DeskConversation }) {
  const { t } = useI18n();
  const shell = useShell();
  const hub = getHub();
  const status = useHubStatus();
  const [state, dispatch] = useReducer(reducer, conversation, (conv) => ({ ...initialState(conv.id), model: conv.model }));
  const [confirmOpus, setConfirmOpus] = useState(false);
  const [refused, setRefused] = useState(false);
  const composer = useRef<ComposerHandle>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const tRef = useRef(t);
  tRef.current = t;

  const client = useRef<HubClient | null>(null);
  if (client.current === null) {
    client.current = { id: conversation.id, ref: null, awaiting: false, onEvent: () => undefined, onOpen: () => undefined, claim: () => undefined };
  }

  const handleEvent = useCallback((event: ServerEvent) => {
    if (event.type === "error" && event.code === BUDGET_EXHAUSTED) {
      // The server refuses to start the turn: the screen says why, in its own words.
      setRefused(true);
      dispatch({ type: "event", event: { ...event, message: "" }, errorText: () => tRef.current("desks.chat.budget.exhausted") });
      void refreshDesks();
      return;
    }
    dispatch({ type: "event", event, errorText });
    // A finished turn changes the tokens of the desk: the meter and the sidebar read them again.
    if (event.type === "done") void refreshDesks();
  }, []);

  useEffect(() => {
    const self = client.current!;
    self.onEvent = handleEvent;
    self.onOpen = () => {
      if (self.id) hub.send({ type: "open", conversation_id: self.id });
    };
    self.claim = () => undefined;
    return hub.register(self);
  }, [hub, handleEvent]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    const last = state.items[state.items.length - 1];
    if (nearBottom || last?.kind === "user") el.scrollTop = el.scrollHeight;
  }, [state.items, state.loading]);

  // Once the budget is raised or the week turns, the box opens again.
  useEffect(() => {
    if (desk.budget_state !== "exhausted") setRefused(false);
  }, [desk.budget_state]);

  const send = (text: string, attachments: AttachmentChip[]): boolean => {
    const message = { type: "send", conversation_id: conversation.id, text, attachments: attachments.map((chip) => chip.id) };
    if (!hub.send(message)) {
      shell.toast({ tone: "warn", text: t("chat.composer.notConnected") });
      return false;
    }
    dispatch({ type: "local_send", text, attachments });
    return true;
  };

  const stop = () => {
    dispatch({ type: "local_stop" });
    hub.send({ type: "stop", conversation_id: conversation.id });
  };

  const answerPermission = (requestId: string, decision: "allow" | "deny") => {
    dispatch({ type: "permission_sending", requestId });
    hub.send({ type: "permission_response", conversation_id: conversation.id, request_id: requestId, decision });
  };

  // The model belongs to the chief in this desk: it is saved there, not in Settings and not for the other desks.
  const pickModel = (model: ModelId) => {
    if (!conversation.agent) return;
    deskAgentsApi.update(desk.id, conversation.agent, { model }).then(
      () => dispatch({ type: "local_model", model }),
      (cause) => shell.toast({ tone: "bad", text: failText(cause, t("desks.chat.modelFailed")) }),
    );
  };
  const requestModel = (model: ModelId) => {
    if (model === "opus" && stateRef.current.model !== "opus") setConfirmOpus(true);
    else pickModel(model);
  };

  const exhausted = desk.budget_state === "exhausted" || refused;
  const pct = budgetPct(desk);

  return (
    <>
      <div className="dterm-ph">
        <span className="dterm-tab on">{t("desks.terminal.chat")}</span>
        <span className="dterm-ph-end">
          {status !== "open" ? (
            <Pill tone="warn" role="status">
              {t("chat.reconnecting")}
            </Pill>
          ) : (
            <Pill>{t(`chat.model.${state.model}`)}</Pill>
          )}
        </span>
      </div>

      <div ref={scroller} className="chat-scroll dterm-chat-scroll" role="log" aria-live="polite" aria-label={t("chat.messages")}>
        {state.loading ? <Loading label={t("chat.loading")} /> : null}
        {state.loadError ? <ErrorState message={state.loadError} onRetry={() => { dispatch({ type: "reset", id: conversation.id }); client.current!.onOpen(); }} /> : null}
        {!state.loading && !state.loadError && state.items.length === 0 ? (
          <EmptyState icon="chat" title={t("desks.chat.empty.title")} description={t("desks.chat.empty.text")} />
        ) : null}
        {state.items.map((item) => (
          <MessageItem
            key={item.key}
            item={item}
            actions={{ projectName: desk.name, canSave: false, onPermission: answerPermission, onSaveMessage: () => undefined, onSaveAttachment: () => undefined }}
          />
        ))}
      </div>

      {desk.budget_state === "warn" && !exhausted ? (
        <div className="dterm-chat-note" role="status">
          <Pill tone="warn">{t("desks.chat.budget.warn", { pct: pct ?? 80 })}</Pill>
        </div>
      ) : null}

      {exhausted ? (
        <div className="dterm-chat-input">
          <p className="hint" role="status">
            {t("desks.chat.budget.exhausted")}
          </p>
          <Input disabled aria-label={t("desks.chat.input")} placeholder={t("desks.chat.placeholder")} />
        </div>
      ) : (
        <Composer
          ref={composer}
          model={state.model}
          running={state.running}
          stopping={state.stopping}
          options={null}
          modelMenuRequest={0}
          usagePct={pct}
          conversationId={conversation.id}
          onModel={requestModel}
          onSend={send}
          onStop={stop}
          host={{ placeholder: t("desks.chat.placeholder"), label: t("desks.chat.input"), tierMenuOnly: true }}
        />
      )}

      {confirmOpus ? (
        <OpusConfirm
          onCancel={() => setConfirmOpus(false)}
          onConfirm={() => {
            setConfirmOpus(false);
            pickModel("opus");
          }}
        />
      ) : null}
    </>
  );
}

