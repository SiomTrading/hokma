import { cx, Dot, Select } from "@/design";
import { useI18n } from "@/i18n";
import { useState } from "react";
import { AssignAccount } from "./AssignAccount";
import { formatBalance, formatCapital, formatPercent, formatResult, resultTone } from "./format";
import { PanelTabs } from "./PanelTabs";
import { accountsOf } from "./types";
import type { DeskTab } from "./panels";
import type { DeskSummary } from "./types";

interface Props {
  desk: DeskSummary;
  desks: DeskSummary[];
  /** The panels of the screen, in the order they are shown. */
  order: DeskTab[];
  tab: DeskTab;
  tabKey: string;
  onTab: (tab: DeskTab) => void;
  onOrder: (order: DeskTab[]) => void;
  onAddPanel: (panel: DeskTab) => void;
  onRemovePanel: (panel: DeskTab) => void;
  /** Panels the "+ Add" menu shows disabled, each with its reason. */
  blocked?: Partial<Record<DeskTab, string>>;
  onPickDesk: (id: number) => void;
}

/** Top bar of a desk: desk selector, its tabs, capital, result of the day and drawdown. */
export function DeskBar({ desk, desks, order, tab, tabKey, onTab, onOrder, onAddPanel, onRemovePanel, blocked, onPickDesk }: Props) {
  const { t, locale } = useI18n();
  const paused = desk.status === "paused";
  const [assigning, setAssigning] = useState(false);
  const accounts = accountsOf(desk);
  const money = desk.capital ?? (accounts.length > 0 ? accounts.reduce((sum, a) => sum + a.initial_capital, 0) : null);
  return (
    <header className="pane desks-hdr desk-bar">
      <h1 className="sr-only">{desk.name}</h1>
      <span className="desk-pick">
        <Dot color={paused ? "var(--warn)" : "var(--ok)"} />
        <Select aria-label={t("desks.bar.pick")} value={String(desk.id)} onChange={(e) => onPickDesk(Number(e.target.value))}>
          {desks.map((item) => (
            <option key={item.id} value={String(item.id)}>
              {item.name}
            </option>
          ))}
        </Select>
      </span>
      <PanelTabs order={order} tab={tab} tabKey={tabKey} onTab={onTab} onOrder={onOrder} onAdd={onAddPanel} onRemove={onRemovePanel} blocked={blocked} />
      <div className="desk-bar-stats">
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.capital")}</span>
          <b className="mono">{money === null ? t("desks.card.noAccount") : formatCapital(money, locale)}</b>
        </div>
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.deskResult")}</span>
          <b className={cx("mono", resultTone(desk.today))}>{formatResult(desk.today, locale)}</b>
        </div>
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.drawdown")}</span>
          <b className="mono">{desk.drawdown === null ? t("desks.stat.none") : formatPercent(desk.drawdown, locale)}</b>
        </div>
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.account")}</span>
          <div className="desk-bar-accounts">
            {accounts.length === 0 ? (
              <b className="mono">{t("desks.card.analysisOnly")}</b>
            ) : (
              accounts.map((account) => (
                <b key={account.id} className="mono">
                  {account.name} · {account.broker} · {formatBalance(account.balance.equity, locale, t("desks.accounts.unavailable"), account.balance.currency)}
                  {account.shared_with && account.shared_with.length > 0 ? (
                    <small>{t(account.shared_with.length === 1 ? "desks.accounts.shared.one" : "desks.accounts.shared.many", { n: String(account.shared_with.length) })}</small>
                  ) : null}
                </b>
              ))
            )}
            <button type="button" className="desk-card-assign" onClick={() => setAssigning(true)}>{t("desks.card.add")}</button>
          </div>
        </div>
        <button type="button" className="pill mono desk-palette" disabled title={t("desks.bar.paletteSoon")} aria-label={t("desks.bar.palette")}>
          ⌕ Ctrl K
        </button>
      </div>
      {assigning ? <AssignAccount desk={desk} onClose={() => setAssigning(false)} /> : null}
    </header>
  );
}
