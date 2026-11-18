import { useState } from "react";
import { Button, cx, EmptyState, ErrorState, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { askHokmaForDesk, CreateDeskModal } from "./CreateDeskModal";
import { DeskCard } from "./DeskCard";
import { formatCapital, formatResult, formatTokens, resultTone } from "./format";
import { refreshDesks, useDesks } from "./store";
import type { DeskSummary } from "./types";

interface Props {
  /** Opens the creation flow right away (the "New desk" row of the menu). */
  openCreate?: boolean;
}

function tokensLine(desks: DeskSummary[], locale: string): string {
  const used = desks.reduce((sum, desk) => sum + desk.tokens_week, 0);
  const capped = desks.length > 0 && desks.every((desk) => desk.token_budget !== null);
  if (!capped) return formatTokens(used, locale);
  const cap = desks.reduce((sum, desk) => sum + (desk.token_budget ?? 0), 0);
  return `${formatTokens(used, locale)} / ${formatTokens(cap, locale)}`;
}

/** The Fund: the numbers of all desks together, one card per desk, and the two ways to create one. */
export function FundView({ openCreate = false }: Props) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const { desks, loaded, loading, error } = useDesks();
  const [creating, setCreating] = useState(openCreate);

  const capital = desks.reduce((sum, desk) => sum + desk.capital, 0);
  const today = desks.reduce((sum, desk) => sum + desk.today, 0);

  let body;
  if (!loaded || (loading && desks.length === 0)) body = <Loading label={t("desks.fund.loading")} />;
  else if (error && desks.length === 0) body = <ErrorState message={error} onRetry={() => void refreshDesks()} />;
  else if (desks.length === 0) {
    body = (
      <EmptyState
        icon="desks"
        title={t("desks.fund.empty.title")}
        description={t("desks.fund.empty.text")}
      />
    );
  } else {
    body = (
      <>
        <div className="desk-cards">
          {desks.map((desk) => (
            <DeskCard key={desk.id} desk={desk} onOpen={() => shell.openScreen("desks", { desk: String(desk.id) })} />
          ))}
        </div>
        <div className="desk-fund-panes">
          <section className="pane desk-fund-pane" aria-label={t("desks.fund.curve")}>
            <h2 className="lbl desk-pane-title">{t("desks.fund.curve")}</h2>
            <EmptyState title={t("desks.fund.curve.empty.title")} description={t("desks.fund.curve.empty.text")} />
          </section>
          <section className="pane desk-fund-pane" aria-label={t("desks.fund.activity")}>
            <h2 className="lbl desk-pane-title">{t("desks.fund.activity")}</h2>
            <EmptyState title={t("desks.fund.activity.empty.title")} description={t("desks.fund.activity.empty.text")} />
          </section>
        </div>
      </>
    );
  }

  return (
    <section className="desks-screen" aria-label={t("desks.fund.title")}>
      <header className="pane desks-hdr">
        <h1 className="desks-title">{t("desks.fund.title")}</h1>
        <div className="ext-tabs" role="tablist" aria-label={t("desks.fund.sections")}>
          <button type="button" role="tab" aria-selected="true" className="ext-tab">
            {t("desks.fund.tab")}
          </button>
          <button type="button" role="tab" aria-selected="false" className="ext-tab" disabled title={t("desks.fund.lessons.soon")}>
            {t("desks.fund.lessons")}
          </button>
        </div>
        <Pill>{t("desks.fund.lessons.soon")}</Pill>
        {desks.length > 0 ? (
          <>
            <div className="desk-stat">
              <span className="lbl">{t("desks.stat.fundCapital")}</span>
              <b className="mono">{formatCapital(capital, locale)}</b>
            </div>
            <div className="desk-stat">
              <span className="lbl">{t("desks.stat.today")}</span>
              <b className={cx("mono", resultTone(today))}>{formatResult(today, locale)}</b>
            </div>
            <div className="desk-stat">
              <span className="lbl">{t("desks.stat.tokensWeek")}</span>
              <b className="mono">{tokensLine(desks, locale)}</b>
            </div>
          </>
        ) : null}
        <span className="desk-hdr-actions">
          <Button onClick={() => askHokmaForDesk(shell, t)}>{t("desks.create.askHokma")}</Button>
          <Button variant="primary" onClick={() => setCreating(true)}>
            {t("desks.create.button")}
          </Button>
        </span>
      </header>
      <div className="desks-body">{body}</div>
      {creating ? <CreateDeskModal onClose={() => setCreating(false)} /> : null}
    </section>
  );
}
