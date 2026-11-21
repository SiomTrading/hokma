import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, ErrorState, Icon, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { AddConnectionDialog } from "./AddConnectionDialog";
import type { DialogView } from "./AddConnectionDialog";
import { connectionErrorText, connectionsApi, mailApi } from "./api";
import type { MailState } from "./api";
import { ConnectedList } from "./ConnectedList";
import "./connections.css";
import { requestFocus, takeLogin, useFocus } from "./focus";
import { SchwabAccountDialog } from "./SchwabAccount";
import { SchwabBand } from "./SchwabBand";
import type { UseKey } from "./types";
import { UseOrder } from "./UseOrder";
import { useConnections } from "./useConnections";

type Dialog = { kind: "add"; start: DialogView; nonce: number } | { kind: "account" } | null;

/** Connections: the "Add" button, what is connected, and the order of use of each kind of data. */
export function ConnectionsScreen() {
  const { t } = useI18n();
  const state = useConnections();
  const focus = useFocus();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [mail, setMail] = useState<MailState | null>(null);
  const [priorityError, setPriorityError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const opened = useRef(0);
  const { cards, uses, setUses, reload, loading } = state;

  const loadMail = useCallback(() => {
    mailApi.state().then(setMail, () => undefined); // Gmail has its own error line inside its card
  }, []);
  useEffect(loadMail, [loadMail]);

  const byId = useMemo(() => new Map(cards.map((card) => [card.id, card])), [cards]);
  const schwab = byId.get("schwab");

  const open = useCallback((start: DialogView) => {
    opened.current += 1;
    setDialog({ kind: "add", start, nonce: opened.current });
  }, []);

  // "Sign in with Schwab" of the notice (login=1): the sign-in step opens by itself, once per request.
  useEffect(() => {
    if (loading || !takeLogin()) return;
    open({ view: "form", id: "schwab", mode: schwab?.added ? "change" : "add", step: schwab?.key_state === "saved" ? 2 : 1 });
  }, [focus.nonce, loading, open, schwab]);

  const focusClass = focus.assetClass;
  useEffect(() => {
    if ((!focusClass && !focus.provider) || loading) return;
    const target = rootRef.current?.querySelector('[data-focus="true"]');
    target?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [focus.nonce, focusClass, focus.provider, loading]);

  const reorder = useCallback(
    (use: UseKey, order: string[]) => {
      const before = uses;
      setUses({ ...uses, [use]: order });
      setPriorityError(null);
      connectionsApi.setPriority(use, order).then(
        (answer) => {
          if (answer.uses) setUses(answer.uses);
          reload();
        },
        (cause: unknown) => {
          setUses(before);
          setPriorityError(connectionErrorText(cause));
        },
      );
    },
    [uses, reload, setUses],
  );

  const close = () => setDialog(null);

  return (
    <Pane as="section" aria-label={t("screen.connections")} style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "auto" }}>
      <div ref={rootRef} className="conn-page">
        <div className="conn-intro conn-header">
          <div className="conn-intro">
            <h1 className="disp">{t("connections.title")}</h1>
            <p className="p">{t("connections.intro2")}</p>
          </div>
          <Button variant="primary" onClick={() => open({ view: "list" })}>
            <Icon name="plus" size={14} /> {t("connections.add")}
          </Button>
        </div>

        {!state.storeAvailable ? (
          <p className="conn-banner" role="alert" data-testid="store-unavailable">
            {t("connections.store.unavailable")}
          </p>
        ) : null}

        <SchwabBand session={schwab?.session} onLogin={() => requestFocus(null, { provider: "schwab", login: true })} />

        {loading ? (
          <Loading label={t("connections.loading")} />
        ) : state.error && cards.length === 0 ? (
          <ErrorState title={t("connections.error.title")} message={state.error} onRetry={reload} />
        ) : (
          <>
            <ConnectedList
              cards={cards}
              mail={mail}
              focusClass={focusClass}
              focusProvider={focus.provider}
              onCard={state.patchCard}
              onReload={reload}
              onStoreMissing={() => state.setStoreAvailable(false)}
              onOpen={open}
              onViewAccount={() => setDialog({ kind: "account" })}
            />
            {priorityError ? (
              <p className="conn-error" role="alert" data-testid="priority-error">
                {priorityError}
              </p>
            ) : null}
            <UseOrder uses={uses} cards={byId} focusUse={focusClass} onReorder={reorder} />
          </>
        )}
      </div>

      {dialog?.kind === "add" ? (
        <AddConnectionDialog
          key={dialog.nonce}
          start={dialog.start}
          cards={cards}
          mail={mail}
          onClose={close}
          onCard={state.patchCard}
          onReload={reload}
          onMailChange={loadMail}
          onStoreMissing={() => state.setStoreAvailable(false)}
        />
      ) : null}
      {dialog?.kind === "account" ? <SchwabAccountDialog onClose={close} /> : null}
    </Pane>
  );
}
