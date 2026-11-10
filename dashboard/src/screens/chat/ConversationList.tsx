import { useCallback, useEffect, useRef, useState } from "react";
import { Button, ErrorState, Input, Loading, Menu, MenuItem, Modal } from "@/design";
import { ApiError } from "@/api";
import { useI18n } from "@/i18n";
import { isPlainClick } from "@/shell/links";
import { conversationOf } from "@/shell/tabsStorage";
import { useShell } from "@/shell/useShell";
import type { ShellApi } from "@/shell/store";
import "./chat.css";
import { deleteConversation, listConversations, patchConversation, relativeTime } from "./chatApi";
import { getHub } from "./hub";
import type { ConversationSummary } from "./types";

const REFRESH_EVENTS = new Set(["done", "conversation", "error"]);

/** Keys of the panel tabs that show a conversation (a draft that became it included). */
function tabKeysOf(shell: ShellApi, conversationId: string): string[] {
  return shell.tabs.filter((tab) => conversationOf(tab) === conversationId).map((tab) => tab.key);
}

function Row({
  conversation,
  active,
  onOpen,
  onChanged,
  onDelete,
}: {
  conversation: ConversationSummary;
  active: boolean;
  onOpen: () => void;
  onChanged: () => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const shell = useShell();
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(conversation.title);
  const trigger = useRef<HTMLButtonElement>(null);
  const title = conversation.title || t("chat.list.untitled");

  const failed = () => shell.toast({ tone: "bad", text: t("chat.list.failed") });

  const togglePin = () => {
    setMenu(false);
    patchConversation(conversation.id, { pinned: !conversation.pinned }).then(onChanged, failed);
  };

  const toggleArchive = () => {
    setMenu(false);
    const archive = !conversation.archived;
    patchConversation(conversation.id, { archived: archive }).then(() => {
      shell.toast({ tone: "ok", text: t(archive ? "chat.list.archived" : "chat.list.unarchived") });
      onChanged();
    }, failed);
  };

  const saveName = () => {
    const name = draft.trim();
    setRenaming(false);
    if (!name || name === conversation.title) return;
    patchConversation(conversation.id, { title: name }).then(() => {
      tabKeysOf(shell, conversation.id).forEach((key) => shell.setTabTitle(key, name));
      onChanged();
    }, failed);
  };

  if (renaming) {
    return (
      <div className="chat-conv-row">
        <label className="sr-only" htmlFor={`rename-${conversation.id}`}>
          {t("chat.list.renameLabel")}
        </label>
        <Input
          id={`rename-${conversation.id}`}
          autoFocus
          maxLength={120}
          style={{ minHeight: 32, fontSize: 12 }}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={saveName}
          onKeyDown={(event) => {
            if (event.key === "Enter") saveName();
            if (event.key === "Escape") {
              event.stopPropagation();
              setRenaming(false);
            }
          }}
        />
      </div>
    );
  }

  return (
    <div className="chat-conv-row" onContextMenu={(event) => { event.preventDefault(); setMenu(true); }}>
      <a
        href={`#/chat/${conversation.id}`}
        className={active ? "conv on" : "conv"}
        aria-current={active ? "page" : undefined}
        onClick={(event) => {
          if (!isPlainClick(event)) return; // wheel, Ctrl, Cmd or Shift: the browser opens it in another tab
          event.preventDefault();
          onOpen();
        }}
      >
        <span className="chat-conv-title">{title}</span>
        <span className="t">{relativeTime(conversation.last_message_at)}</span>
      </a>
      <button ref={trigger} type="button" className="chat-conv-more" aria-label={t("chat.list.options", { title })} aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
        ···
      </button>
      <Menu open={menu} onClose={() => setMenu(false)} label={t("chat.list.options", { title })} triggerRef={trigger} className="chat-conv-menu">
        {conversation.archived ? null : <MenuItem onClick={togglePin}>{conversation.pinned ? t("chat.list.unpin") : t("chat.list.pin")}</MenuItem>}
        {conversation.archived ? null : (
          <MenuItem onClick={() => { setMenu(false); setDraft(conversation.title); setRenaming(true); }}>{t("chat.list.rename")}</MenuItem>
        )}
        <MenuItem onClick={toggleArchive}>{conversation.archived ? t("chat.list.unarchive") : t("chat.list.archive")}</MenuItem>
        <MenuItem onClick={() => { setMenu(false); onDelete(); }}>{t("chat.list.delete")}</MenuItem>
      </Menu>
    </div>
  );
}

/** Slot `sidebar.conversations`: pinned and recent conversations from the database, with search. */
export function ConversationList({ query }: { query: string }) {
  const { t } = useI18n();
  const shell = useShell();
  const [items, setItems] = useState<ConversationSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [toDelete, setToDelete] = useState<ConversationSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const queryRef = useRef(query);
  queryRef.current = query;
  const archivedRef = useRef(showArchived);
  archivedRef.current = showArchived;
  const abort = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    listConversations(queryRef.current, controller.signal, archivedRef.current).then(
      (rows) => {
        setItems(rows);
        setFailed(false);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) return;
        setFailed(true);
      },
    );
  }, []);

  useEffect(() => {
    const timer = setTimeout(load, query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [query, load, showArchived]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = getHub().watch((event) => {
      if (!REFRESH_EVENTS.has(event.type)) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 300);
    });
    return () => {
      stop();
      if (timer) clearTimeout(timer);
      abort.current?.abort();
    };
  }, [load]);

  const activeTab = shell.tabs.find((tab) => tab.key === shell.activeKey);
  const activeId = activeTab ? conversationOf(activeTab) : null;

  const confirmDelete = () => {
    if (!toDelete) return;
    setBusy(true);
    deleteConversation(toDelete.id).then(
      () => {
        tabKeysOf(shell, toDelete.id).forEach((key) => shell.closeTab(key));
        setToDelete(null);
        setBusy(false);
        load();
      },
      (error: unknown) => {
        setBusy(false);
        setToDelete(null);
        shell.toast({ tone: "bad", text: error instanceof ApiError && error.status === 409 ? t("chat.list.deleteBusy") : t("chat.list.failed") });
      },
    );
  };

  if (failed && items === null) return <ErrorState title={t("chat.list.error")} onRetry={load} className="chat-list-state" />;
  if (items === null) return <Loading inline className="chat-list-state" />;

  const searching = query.trim() !== "";
  const pinned = items.filter((c) => c.pinned);
  const recent = items.filter((c) => !c.pinned);

  const renderRows = (list: ConversationSummary[]) =>
    list.map((conversation) => (
      <Row
        key={conversation.id}
        conversation={conversation}
        active={activeId === conversation.id}
        onOpen={() => shell.openScreen("chat", { conversation: conversation.id })}
        onChanged={load}
        onDelete={() => setToDelete(conversation)}
      />
    ));

  return (
    <>
      {failed ? <ErrorState title={t("chat.list.error")} onRetry={load} className="chat-list-state" /> : null}
      {showArchived ? (
        <>
          <span className="sec">{t("chat.list.archivedTitle")}</span>
          {items.length === 0 ? <p className="p chat-list-empty">{searching ? t("chat.list.noResults") : t("chat.list.archivedEmpty")}</p> : renderRows(items)}
        </>
      ) : items.length === 0 ? (
        <p className="p chat-list-empty">{searching ? t("chat.list.noResults") : `${t("chat.list.empty")}. ${t("chat.list.emptyText")}`}</p>
      ) : searching ? (
        <>
          <span className="sec">{t("chat.list.results")}</span>
          {renderRows(items)}
        </>
      ) : (
        <>
          {pinned.length > 0 ? (
            <>
              <span className="sec">{t("chat.list.pinned")}</span>
              {renderRows(pinned)}
            </>
          ) : null}
          {recent.length > 0 ? (
            <>
              <span className="sec">{t("chat.list.recent")}</span>
              {renderRows(recent)}
            </>
          ) : null}
        </>
      )}
      <button type="button" className="chat-link chat-list-more" onClick={() => setShowArchived((v) => !v)}>
        {showArchived ? t("chat.list.archivedHide") : t("chat.list.archivedShow")}
      </button>
      {toDelete ? (
        <Modal label={t("chat.list.deleteTitle")} onClose={() => setToDelete(null)} style={{ width: "min(420px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
          <h2 className="h1">{t("chat.list.deleteTitle")}</h2>
          <p className="p">{t("chat.list.deleteText")}</p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setToDelete(null)}>
              {t("chat.list.cancel")}
            </Button>
            <Button variant="danger" disabled={busy} onClick={confirmDelete}>
              {t("chat.list.deleteConfirm")}
            </Button>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
