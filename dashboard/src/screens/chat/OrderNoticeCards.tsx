import { useEffect, useState, useSyncExternalStore } from "react";
import { ApiError } from "@/api";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { getHub } from "./hub";
import { capApi, noticeApi } from "./orderNotices";
import type { CapNotice, ClosePlan, NoticeScope, OrderNotice } from "./orderNotices";

/** Minutes left until `expiresAt` (UTC text), rounded up; 0 or less means it ended. */
export function noticeMinutesLeft(expiresAt: string | null, now: number): number {
  if (!expiresAt) return 0;
  const end = Date.parse(expiresAt.replace(" ", "T") + "Z");
  return Number.isNaN(end) ? 0 : Math.ceil((end - now) / 60000);
}

/**
 * The three answers of a notice: Follow, No and Close. Follow asks the scope EVERY time (only this order, or the rest of the day: a temporary permission
 * that ends with the session and never changes the rule). Used by the card of the chat and by the pending orders of the desk, so both say the same.
 */
export function NoticeActions({ proposalId, label, onDone }: { proposalId: number; label: string; onDone?: () => void }) {
  const { t } = useI18n();
  const [asking, setAsking] = useState(false);
  const [closing, setClosing] = useState<ClosePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (call: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await call();
      onDone?.();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("chat.notice.error"));
      setAsking(false);
      setClosing(null);
    }
    setBusy(false);
  };

  /** Close only asks: it shows what would be closed; nothing is sent until the confirm button. */
  const ask = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setClosing(await noticeApi.close(proposalId));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("chat.notice.error"));
    }
    setBusy(false);
  };

  if (closing) {
    return (
      <div className="chat-notice-actions" role="group" aria-label={t("chat.notice.closeAria", { order: label })}>
        <p className="p mono chat-notice-plan">{closing.text}</p>
        <div className="desk-hdr-actions">
          <Button variant="primary" size="sm" disabled={busy} onClick={() => void run(() => noticeApi.confirmClose(proposalId, closing.plan_token))}>{t("chat.notice.closeConfirm")}</Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setClosing(null)}>{t("chat.notice.closeBack")}</Button>
        </div>
        {error ? <p className="p chat-notice-error" role="alert">{error}</p> : null}
      </div>
    );
  }
  if (asking) {
    return (
      <div className="chat-notice-actions" role="group" aria-label={t("chat.notice.scopeAria", { order: label })}>
        <p className="p">{t("chat.notice.scopeAsk")}</p>
        <div className="desk-hdr-actions">
          <Button variant="primary" size="sm" disabled={busy} onClick={() => void run(() => noticeApi.follow(proposalId, "order" as NoticeScope))}>{t("chat.notice.onlyThis")}</Button>
          <Button variant="primary" size="sm" disabled={busy} onClick={() => void run(() => noticeApi.follow(proposalId, "day" as NoticeScope))}>{t("chat.notice.restOfDay")}</Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(() => noticeApi.no(proposalId))}>{t("chat.notice.no")}</Button>
        </div>
        {error ? <p className="p chat-notice-error" role="alert">{error}</p> : null}
      </div>
    );
  }
  return (
    <div className="chat-notice-actions" role="group" aria-label={t("chat.notice.actionsAria", { order: label })}>
      <div className="desk-hdr-actions">
        <Button variant="primary" size="sm" disabled={busy} onClick={() => setAsking(true)}>{t("chat.notice.follow")}</Button>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(() => noticeApi.no(proposalId))}>{t("chat.notice.no")}</Button>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void ask()}>{t("chat.notice.close")}</Button>
      </div>
      {error ? <p className="p chat-notice-error" role="alert">{error}</p> : null}
    </div>
  );
}

/** The three answers of a notice of a cap with no order: keep trading today, block entries today, close positions (two steps: the list first, then the confirmation). */
export function CapActions({ capId, label, onDone }: { capId: number; label: string; onDone?: () => void }) {
  const { t } = useI18n();
  const [closing, setClosing] = useState<{ text: string; plan_token: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (call: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await call();
      onDone?.();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("chat.notice.error"));
      setClosing(null);
    }
    setBusy(false);
  };

  const ask = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setClosing(await capApi.close(capId));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("chat.notice.error"));
    }
    setBusy(false);
  };

  if (closing) {
    return (
      <div className="chat-notice-actions" role="group" aria-label={t("chat.notice.closeAria", { order: label })}>
        <p className="p mono chat-notice-plan">{closing.text}</p>
        <div className="desk-hdr-actions">
          <Button variant="primary" size="sm" disabled={busy} onClick={() => void run(() => capApi.confirmClose(capId, closing.plan_token))}>{t("chat.notice.closeConfirm")}</Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(async () => { await capApi.back(capId); setClosing(null); })}>{t("chat.notice.closeBack")}</Button>
        </div>
        {error ? <p className="p chat-notice-error" role="alert">{error}</p> : null}
      </div>
    );
  }
  return (
    <div className="chat-notice-actions" role="group" aria-label={t("chat.cap.actionsAria", { account: label })}>
      <div className="desk-hdr-actions">
        <Button variant="primary" size="sm" disabled={busy} onClick={() => void run(() => capApi.follow(capId))}>{t("chat.cap.follow")}</Button>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(() => capApi.block(capId))}>{t("chat.cap.block")}</Button>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void ask()}>{t("chat.cap.close")}</Button>
      </div>
      {error ? <p className="p chat-notice-error" role="alert">{error}</p> : null}
    </div>
  );
}

function CapCard({ notice, now }: { notice: CapNotice; now: number }) {
  const { t } = useI18n();
  const left = noticeMinutesLeft(notice.expires_at, now);
  const where = [notice.desk, notice.account].filter(Boolean).join(" ");
  const what = notice.metric === "drawdown" ? t("chat.cap.drawdown") : t("chat.cap.loss");
  return (
    <li className="chat-notice" aria-label={t("chat.cap.cardAria", { where })} data-testid="cap-notice">
      <div className="chat-notice-hd">
        <b>{t("chat.cap.title", { where })}</b>
        <span className="today-meta">{left > 0 ? t("chat.notice.left", { n: String(left) }) : t("chat.notice.over")}</span>
      </div>
      <p className="p"><span className="lbl">{t("chat.notice.rule")}</span> {notice.rule_id}</p>
      <p className="p">{t("chat.cap.reached", { what, value: notice.value, cap: notice.cap })}</p>
      {notice.read_at ? <p className="p today-meta">{t("chat.cap.readAt", { at: notice.read_at })}</p> : null}
      <p className="p today-meta">{t("chat.cap.nothingDone")}</p>
      <CapActions capId={notice.cap_id} label={where} />
    </li>
  );
}

function Card({ notice, now }: { notice: OrderNotice; now: number }) {
  const { t } = useI18n();
  const left = noticeMinutesLeft(notice.expires_at, now);
  const where = [notice.desk, notice.account].filter(Boolean).join(" ");
  return (
    <li className="chat-notice" aria-label={t("chat.notice.cardAria", { where })}>
      <div className="chat-notice-hd">
        <b>{t("chat.notice.title", { where })}</b>
        <span className="today-meta">{left > 0 ? t("chat.notice.left", { n: String(left) }) : t("chat.notice.over")}</span>
      </div>
      <p className="p"><span className="lbl">{t("chat.notice.rule")}</span> {notice.rules.join(", ")}</p>
      {notice.whys.map((why) => <p className="p" key={why}>{why}</p>)}
      {notice.summary ? <p className="p mono">{notice.summary}</p> : null}
      <p className="p today-meta">{t("chat.notice.neverSent")}</p>
      <NoticeActions proposalId={notice.proposal_id} label={where} />
    </li>
  );
}

/** The actionable cards of the chat: what asks a decision of the owner shows here, whatever conversation is open (the panel shows the same list). */
export function OrderNoticeCards() {
  const store = getHub().notices;
  const items = useSyncExternalStore(store.subscribe, store.get);
  const caps = useSyncExternalStore(store.subscribe, store.getCaps);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (items.length + caps.length === 0) return;
    const timer = setInterval(() => setNow(Date.now()), 15000);      // only the local clock, to count the minutes down
    return () => clearInterval(timer);
  }, [items.length, caps.length]);
  if (items.length + caps.length === 0) return null;
  return (
    <ul className="chat-notices" aria-live="polite">
      {caps.map((notice) => <CapCard key={`cap-${notice.cap_id}`} notice={notice} now={now} />)}
      {items.map((notice) => <Card key={notice.proposal_id} notice={notice} now={now} />)}
    </ul>
  );
}
