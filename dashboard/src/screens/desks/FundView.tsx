import { useState } from "react";
import { Button, cx, EmptyState, ErrorState, Loading } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { BrokerView } from "./BrokerView";
import { askHokmaForDesk, CreateDeskModal } from "./CreateDeskModal";
import { DeskCard } from "./DeskCard";
import { formatBalance, formatCapital, formatResult, resultTone } from "./format";
import { refreshDesks, useDesks } from "./store";
import { TodayCard } from "./TodayCard";
import type { DeskSummary } from "./types";

interface Props {
  /** Opens the creation flow right away (the "New desk" row of the menu). */
  openCreate?: boolean;
}

/** The numbers of the header, from the accounts assigned to desks. Equity is null when any broker cannot tell. */
function fundTotals(desks: DeskSummary[]) {
  const accounts = desks.flatMap((d) => (d.account ? [d.account] : []));
  const capital = accounts.reduce((sum, a) => sum + a.initial_capital, 0);
  const equity = accounts.some((a) => a.balance.equity === null) ? null : accounts.reduce((sum, a) => sum + (a.balance.equity ?? 0), 0);
  const today = desks.reduce((sum, d) => sum + d.today, 0);
  return { accounts: accounts.length, capital, equity, today, currency: accounts[0]?.balance.currency ?? "USD" };
}

/** The Fund: the numbers of all desks together, the Today card and one card per desk; the Broker tab holds the accounts. */
export function FundView({ openCreate = false }: Props) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const { desks, loaded, loading, error } = useDesks();
  const [creating, setCreating] = useState(openCreate);
  const [section, setSection] = useState<"fund" | "broker">("fund");
  const totals = fundTotals(desks);
  const pct = totals.capital > 0 ? (totals.today / totals.capital) * 100 : 0;
  const tab = t(section === "fund" ? "desks.fund.tab" : "desks.fund.broker");

  let desksBody;
  if (!loaded || (loading && desks.length === 0)) desksBody = <Loading label={t("desks.fund.loading")} />;
  else if (error && desks.length === 0) desksBody = <ErrorState message={error} onRetry={() => void refreshDesks()} />;
  else if (desks.length === 0) {
    desksBody = (
      <EmptyState icon="desks" title={t("desks.fund.empty.title")} description={t("desks.fund.empty.text")}
        action={<Button variant="primary" onClick={() => setCreating(true)}>{t("desks.fund.empty.button")}</Button>} />
    );
  } else {
    desksBody = (
      <div className="desk-cards">
        {desks.map((desk) => (
          <DeskCard key={desk.id} desk={desk} onOpen={() => shell.openScreen("desks", { desk: String(desk.id) })} />
        ))}
      </div>
    );
  }

  return (
    <section className="desks-screen" aria-label={t("desks.fund.title")}>
      <header className="pane desks-hdr">
        <div className="desks-hdr-top">
          <div className="desks-hdr-title">
            <span className="desk-crumb">{t("desks.fund.title")} / {tab}</span>
            <h1 className="disp">{tab}</h1>
          </div>
          <div className="ext-tabs" role="tablist" aria-label={t("desks.fund.sections")}>
            <button type="button" role="tab" aria-selected={section === "fund"} className="ext-tab" onClick={() => setSection("fund")}>
              {t("desks.fund.tab")}
            </button>
            <button type="button" role="tab" aria-selected={section === "broker"} className="ext-tab" onClick={() => setSection("broker")}>
              {t("desks.fund.broker")}
            </button>
            <button type="button" role="tab" aria-selected="false" className="ext-tab" disabled>
              {t("desks.fund.lessons")}
            </button>
          </div>
          <span className="desk-hdr-actions">
            <Button onClick={() => askHokmaForDesk(shell, t)}>{t("desks.create.askHokma")}</Button>
            <Button variant="primary" onClick={() => setCreating(true)}>{t("desks.create.button")}</Button>
          </span>
        </div>
        {section === "fund" && loaded ? (
          <div className="desk-kpis">
            <div className="desk-kpi desk-kpi-main">
              <span className="lbl">{t("desks.stat.fundCapital")}</span>
              <b className="mono">{formatCapital(totals.capital, locale)} <small>{totals.currency}</small></b>
              <span className="desk-kpi-sub">{t("desks.kpi.capitalOf", { n: String(totals.accounts) })}</span>
            </div>
            <div className="desk-kpi">
              <span className="lbl">{t("desks.kpi.dayChange")}</span>
              <b className={cx("mono", resultTone(totals.today))}>{formatResult(totals.today, locale)}</b>
              <span className="desk-kpi-sub">{formatResult(pct, locale)}% · {t(totals.today === 0 ? "desks.kpi.noOps" : "desks.kpi.ofCapital")}</span>
            </div>
            <div className="desk-kpi">
              <span className="lbl">{t("desks.kpi.equity")}</span>
              <b className="mono">{formatBalance(totals.equity, locale, t("desks.accounts.unavailable"))}</b>
              <span className="desk-kpi-sub">{t("desks.kpi.equityNote")}</span>
            </div>
          </div>
        ) : null}
      </header>
      <div className="desks-body">
        {section === "broker" ? (
          <BrokerView />
        ) : (
          <>
            <TodayCard />
            <section aria-labelledby="fund-desks-title" className="desk-fund-desks">
              <h2 id="fund-desks-title" className="desk-sec-title">{t("desks.fund.desks")}{desks.length ? <em>{desks.length}</em> : null}</h2>
              {desksBody}
            </section>
          </>
        )}
      </div>
      {creating ? <CreateDeskModal onClose={() => setCreating(false)} /> : null}
    </section>
  );
}
