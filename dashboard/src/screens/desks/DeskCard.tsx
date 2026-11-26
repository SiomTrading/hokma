import { useRef, useState } from "react";
import { cx, Menu, MenuItem, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { AssignAccount } from "./AssignAccount";
import { DeleteDeskModal } from "./DeleteDeskModal";
import { formatBalance, formatCapital, formatPercent, formatResult, resultTone } from "./format";
import { accountsOf } from "./types";
import type { DeskSummary } from "./types";

interface Props {
  desk: DeskSummary;
  onOpen: () => void;
}

/** One desk in the Fund: name, focus, state, capital, result of the day, drawdown and agents working. */
export function DeskCard({ desk, onOpen }: Props) {
  const { t, locale } = useI18n();
  const [menu, setMenu] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [assigning, setAssigning] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const paused = desk.status === "paused";
  const today = formatResult(desk.today, locale);
  const accounts = accountsOf(desk);
  const money = desk.capital ?? (accounts.length > 0 ? accounts.reduce((sum, a) => sum + a.initial_capital, 0) : null);
  return (
    <div className="desk-card-wrap">
    <button type="button" className="pane desk-card" onClick={onOpen} aria-label={t("desks.card.open", { name: desk.name })}>
      <div className="desk-card-top">
        <div className="desk-card-id">
          <span className="desk-card-name">{desk.name}</span>
          {desk.focus ? <span className="desk-card-focus">{desk.focus}</span> : null}
        </div>
        <Pill tone={paused ? "warn" : "ok"}>{t(paused ? "desks.status.paused" : "desks.status.active")}</Pill>
      </div>
      <div className="desk-card-stats">
        <div>
          <div className="lbl">{t("desks.stat.capital")}</div>
          <div className="mono">{money === null ? t("desks.card.noAccount") : formatCapital(money, locale)}</div>
        </div>
        <div>
          <div className="lbl">{t("desks.stat.deskResult")}</div>
          <div className={cx("mono", resultTone(desk.today))}>{today}</div>
        </div>
        <div>
          <div className="lbl">{t("desks.stat.drawdown")}</div>
          <div className="mono">{desk.drawdown === null ? t("desks.stat.none") : formatPercent(desk.drawdown, locale)}</div>
        </div>
      </div>
      <div className="desk-card-accounts">
        {accounts.length === 0 ? (
          <span className="desk-card-acct-none">{t("desks.card.analysisOnly")}</span>
        ) : (
          accounts.map((account) => (
            <div key={account.id} className="desk-card-account">
              <span className="desk-card-acct-name">
                {account.name}<small>{account.broker}{account.mask ? ` ${account.mask}` : ""}</small>
                {account.shared_with && account.shared_with.length > 0 ? (
                  <small className="desk-card-shared">{t(account.shared_with.length === 1 ? "desks.accounts.shared.one" : "desks.accounts.shared.many", { n: String(account.shared_with.length) })}</small>
                ) : null}
              </span>
              <span className="desk-card-acct-nums">
                <span><span className="lbl">{t("desks.accounts.balance")}</span><b className="mono">{formatBalance(account.balance.cash, locale, t("desks.accounts.unavailable"))}</b></span>
                <span><span className="lbl">{t("desks.kpi.equity")}</span><b className="mono">{formatBalance(account.balance.equity, locale, t("desks.accounts.unavailable"))}</b></span>
              </span>
            </div>
          ))
        )}
      </div>
      <div className="desk-card-foot">
        <span>{t("desks.card.working", { n: String(desk.agents_working), total: String(desk.agents_total) })}</span>
        <span className="desk-card-pills">
          {desk.budget_state !== "ok" ? (
            <Pill tone={desk.budget_state === "exhausted" ? "bad" : "warn"}>{t(`desks.budget.${desk.budget_state}`)}</Pill>
          ) : null}
        </span>
      </div>
    </button>
      <button type="button" className="desk-card-assign" onClick={() => setAssigning(true)}>
        {t("desks.card.add")}
      </button>
      <button ref={trigger} type="button" className="desk-card-more" aria-label={t("desks.card.options", { name: desk.name })} aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
        ···
      </button>
      <Menu open={menu} onClose={() => setMenu(false)} label={t("desks.card.options", { name: desk.name })} triggerRef={trigger} className="desk-card-menu">
        <MenuItem onClick={() => { setMenu(false); setDeleting(true); }}>{t("desks.delete.button")}</MenuItem>
      </Menu>
      {assigning ? <AssignAccount desk={desk} onClose={() => setAssigning(false)} /> : null}
      {deleting ? <DeleteDeskModal desk={desk} onClose={() => setDeleting(false)} /> : null}
    </div>
  );
}
