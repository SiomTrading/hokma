import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { Button, Dot, Menu, MenuItem, Modal, Pill } from "@/design";
import { useI18n } from "@/i18n";
import type { DialogView } from "./AddConnectionDialog";
import { MAIL_ID, whatText } from "./AddConnectionDialog";
import { codeText, connectionErrorText, connectionsApi, isStoreUnavailable } from "./api";
import type { MailState } from "./api";
import { daysLeft } from "./session";
import type { AssetClass, ConnectionCard, DataState, TestResult } from "./types";

/** Yahoo Finance is the fixed fallback: it is not in the catalog of cards, it has no menu and it cannot be turned off. */
export const FALLBACK_ID = "yahoo";

const STATE_TONE = { connected: "ok", reconnecting: "warn", error: "bad", not_connected: "default" } as const;

interface Props {
  /** Every card of the list; the connected ones are drawn here. */
  cards: ConnectionCard[];
  mail: MailState | null;
  focusClass: AssetClass | null;
  focusProvider: string | null;
  onCard: (card: ConnectionCard) => void;
  onReload: () => void;
  onStoreMissing: () => void;
  onOpen: (view: DialogView) => void;
  onViewAccount: () => void;
  onDiagnostic: () => void;
}

/** "Connected": one row per connected source, its menu, and Yahoo Finance fixed at the end. */
export function ConnectedList({ cards, mail, focusClass, focusProvider, onCard, onReload, onStoreMissing, onOpen, onViewAccount, onDiagnostic }: Props) {
  const { t } = useI18n();
  const [removing, setRemoving] = useState<ConnectionCard | null>(null);
  const connected = cards.filter((card) => card.added && card.id !== FALLBACK_ID);
  const providerRows = connected.map((card) => (
    <ProviderRow
      key={card.id}
      card={card}
      focused={focusProvider === card.id || (focusClass !== null && card.asset_classes.includes(focusClass))}
      onCard={onCard}
      onStoreMissing={onStoreMissing}
      onOpen={onOpen}
      onViewAccount={onViewAccount}
      onDiagnostic={onDiagnostic}
      onRemove={() => setRemoving(card)}
    />
  ));
  // The empty message waits for a source that gives stocks or futures: Coinbase alone is not "a live source" for them.
  const hasPriceSource = connected.some((card) => card.asset_classes.includes("equity") || card.asset_classes.includes("future"));

  return (
    <section className="conn-section" aria-label={t("connections.connected.title")} data-testid="connected-list">
      <h2 className="sec conn-group-title">{t("connections.connected.title")}</h2>
      <ul className="conn-list">
        {providerRows}
        {mail?.connected ? <MailRow email={mail.email ?? ""} onOpen={onOpen} /> : null}
        <li className="conn-row conn-row-fixed" data-testid="connection-yahoo" data-provider={FALLBACK_ID}>
          <span className="conn-dot-off" aria-hidden="true" />
          <div className="conn-row-main">
            <span className="h2">Yahoo Finance</span>
            <span className="p conn-what">{t("connections.fallback.yahoo")}</span>
          </div>
          <Pill tone="warn">{t("connections.data.delayed")}</Pill>
        </li>
      </ul>
      {!hasPriceSource ? (
        <p className="p" data-testid="connected-empty">
          {t("connections.connected.empty")}
        </p>
      ) : null}
      {removing ? (
        <RemoveConfirm
          card={removing}
          onClose={() => setRemoving(null)}
          onCard={onCard}
          onReload={onReload}
          onStoreMissing={onStoreMissing}
        />
      ) : null}
    </section>
  );
}

interface RowProps {
  card: ConnectionCard;
  focused: boolean;
  onCard: (card: ConnectionCard) => void;
  onStoreMissing: () => void;
  onOpen: (view: DialogView) => void;
  onViewAccount: () => void;
  onDiagnostic: () => void;
  onRemove: () => void;
}

function RowMenu({ name, children }: { name: string; children: (close: () => void) => ReactNode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const label = t("connections.row.menu", { name });
  return (
    <div className="conn-menu-wrap" ref={wrap}>
      <Button size="sm" variant="ghost" aria-haspopup="menu" aria-expanded={open} aria-label={label} onClick={() => setOpen((value) => !value)}>
        <span aria-hidden="true">...</span>
      </Button>
      <Menu open={open} onClose={() => setOpen(false)} label={label} triggerRef={wrap} className="conn-menu" placement="below">
        {children(() => setOpen(false))}
      </Menu>
    </div>
  );
}

function ProviderRow({ card, focused, onCard, onStoreMissing, onOpen, onViewAccount, onDiagnostic, onRemove }: RowProps) {
  const { t } = useI18n();
  const [busy, setBusy] = useState<"test" | "toggle" | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dataLabel = (state: DataState) => (state === "live" ? t("connections.data.live") : t("connections.data.delayed"));
  const oauth = card.auth === "oauth";
  const session = card.session ?? null;
  const expired = oauth && session?.state === "expired";
  const errorText = expired ? t("connections.err.schwab_expired") : card.state === "error" ? codeText(card.error_code, card.error_message) : "";
  const mask = session?.accounts?.[0]?.mask;
  const days = oauth && (session?.state === "signed_in" || session?.state === "expiring") ? daysLeft(session) : 0;
  const detail = oauth && mask ? t("connections.schwab.account", { mask }) : whatText(t, card.id);

  const fail = (cause: unknown) => {
    if (isStoreUnavailable(cause)) onStoreMissing();
    setError(connectionErrorText(cause));
  };

  const runTest = async () => {
    setBusy("test");
    setError(null);
    setTest(null);
    try {
      setTest(await connectionsApi.test(card.id));
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  const toggle = async () => {
    setBusy("toggle");
    setError(null);
    try {
      onCard(await connectionsApi.setEnabled(card.id, !card.enabled));
    } catch (cause) {
      fail(cause);
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className={"conn-row" + (focused ? " focus" : "")} data-testid={"connection-" + card.id} data-provider={card.id} data-focus={focused ? "true" : undefined}>
      <Dot live={card.enabled && card.state === "connected"} />
      <div className="conn-row-main">
        <span className="h2">{card.name}</span>
        {detail ? (
          <span className="p conn-what" data-testid="conn-detail">
            {detail}
          </span>
        ) : null}
        {errorText ? (
          <span className="conn-error" role="alert" data-testid="conn-error">
            {errorText}
          </span>
        ) : null}
      </div>
      <div className="conn-pills">
        {!card.enabled ? (
          <Pill tone="warn" data-testid="conn-state" data-state="paused">
            {t("connections.row.paused")}
          </Pill>
        ) : (
          <>
            <Pill tone={STATE_TONE[card.state]} data-testid="conn-state" data-state={card.state}>
              {card.state === "connected"
                ? t("connections.state.connected")
                : card.state === "reconnecting"
                  ? t("connections.state.reconnecting")
                  : card.state === "error"
                    ? t("connections.state.error")
                    : t("connections.state.notConnected")}
            </Pill>
            {card.state === "connected" || card.state === "reconnecting" ? (
              <Pill tone={card.data_state === "live" ? "acc" : "warn"} data-testid="conn-data" data-data-state={card.data_state}>
                {dataLabel(card.data_state)}
              </Pill>
            ) : null}
          </>
        )}
        {days > 0 ? (
          <Pill data-testid="conn-session">{t("connections.schwab.expiresIn", { days })}</Pill>
        ) : null}
      </div>
      <RowMenu name={card.name}>
        {(close) => (
          <>
            <MenuItem disabled={busy !== null || !card.available} onClick={() => { close(); void runTest(); }}>
              {busy === "test" ? t("connections.test.running") : t("connections.test")}
            </MenuItem>
            <MenuItem disabled={busy !== null} onClick={() => { close(); void toggle(); }}>
              {card.enabled ? t("connections.row.pause") : t("connections.row.resume")}
            </MenuItem>
            {card.needs_key ? (
              <MenuItem onClick={() => { close(); onOpen({ view: "form", id: card.id, mode: "change", step: 1 }); }}>{t("connections.row.changeKey")}</MenuItem>
            ) : null}
            {oauth ? (
              <>
                <MenuItem onClick={() => { close(); onOpen({ view: "form", id: card.id, mode: "change", step: 2 }); }}>{t("connections.schwab.login")}</MenuItem>
                <MenuItem onClick={() => { close(); onViewAccount(); }}>{t("connections.schwab.viewAccount")}</MenuItem>
                <MenuItem onClick={() => { close(); onDiagnostic(); }}>{t("connections.schwab.diagnostic")}</MenuItem>
              </>
            ) : null}
            <MenuItem onClick={() => { close(); onOpen({ view: "guide", id: card.id }); }}>{t("connections.row.guide")}</MenuItem>
            <MenuItem onClick={() => { close(); onRemove(); }}>{t("connections.row.remove")}</MenuItem>
          </>
        )}
      </RowMenu>
      {test ? (
        <p className={"conn-result conn-row-wide " + (test.ok ? "ok" : "bad")} role="status" data-testid="conn-test-result" data-ok={test.ok ? "true" : "false"}>
          {test.ok
            ? test.latency_ms === null
              ? t("connections.test.okNoLatency", { data: dataLabel(test.data_state) })
              : t("connections.test.ok", { data: dataLabel(test.data_state), ms: Math.round(test.latency_ms) })
            : t("connections.test.fail", { reason: codeText(test.error_code, test.message) })}
        </p>
      ) : null}
      {error ? (
        <p className="conn-error conn-row-wide" role="alert" data-testid="conn-action-error">
          {error}
        </p>
      ) : null}
    </li>
  );
}

function MailRow({ email, onOpen }: { email: string; onOpen: (view: DialogView) => void }) {
  const { t } = useI18n();
  return (
    <li className="conn-row" data-testid="connection-mail" data-provider={MAIL_ID}>
      <Dot live />
      <div className="conn-row-main">
        <span className="h2">{t("mail.title")}</span>
        <span className="p conn-what" data-testid="conn-detail">
          {email}
        </span>
      </div>
      <div className="conn-pills">
        <Pill tone="ok" data-testid="conn-state" data-state="connected">
          {t("connections.state.connected")}
        </Pill>
      </div>
      <RowMenu name={t("mail.title")}>
        {(close) => (
          <>
            <MenuItem onClick={() => { close(); onOpen({ view: "guide", id: MAIL_ID }); }}>{t("connections.row.guide")}</MenuItem>
            <MenuItem onClick={() => { close(); onOpen({ view: "form", id: MAIL_ID, mode: "change" }); }}>{t("mail.disconnect")}</MenuItem>
          </>
        )}
      </RowMenu>
    </li>
  );
}

/** "Remove {name}": asks first, then removes the key and the session (4.3 rule 4). */
function RemoveConfirm({ card, onClose, onCard, onReload, onStoreMissing }: { card: ConnectionCard; onClose: () => void; onCard: (card: ConnectionCard) => void; onReload: () => void; onStoreMissing: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      onCard(await connectionsApi.remove(card.id));
      onReload();
      onClose();
    } catch (cause) {
      if (isStoreUnavailable(cause)) onStoreMissing();
      setError(connectionErrorText(cause));
      setBusy(false);
    }
  };

  return (
    <Modal label={t("connections.row.remove") + " " + card.name} role="alertdialog" onClose={onClose} className="conn-confirm">
      <div className="conn-dialog-body">
        <p className="p" data-testid="remove-confirm">
          {t("connections.row.removeConfirm", { name: card.name })}
        </p>
        {error ? (
          <p className="conn-error" role="alert" data-testid="remove-error">
            {error}
          </p>
        ) : null}
        <div className="conn-actions conn-end">
          <Button onClick={onClose} disabled={busy}>
            {t("connections.row.cancel")}
          </Button>
          <Button variant="danger" onClick={() => void remove()} disabled={busy}>
            {t("connections.row.remove")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
