import { Meter } from "@/design";
import { useI18n } from "@/i18n";
import type { DeskUsage } from "./agentsApi";
import { formatTokens } from "./format";

interface Props {
  usage: DeskUsage;
}

/** Weekly token budget of the desk: used against the cap, amber from 80%, red at 100%. Tokens only, never money. */
export function BudgetMeter({ usage }: Props) {
  const { t, locale } = useI18n();
  const cap = usage.token_budget;
  const pct = cap !== null ? (usage.pct ?? Math.round((usage.used / cap) * 100)) : 0;
  const tone = usage.state === "exhausted" ? "bad" : usage.state === "warn" ? "warn" : "acc";
  return (
    <div className="desk-budget">
      <div className="kv">
        <span className="lbl">{t("desks.agents.budget")}</span>
        <span className="mono">
          {cap !== null ? `${formatTokens(usage.used, locale)} / ${formatTokens(cap, locale)}` : t("desks.agents.budget.none")}
        </span>
      </div>
      {cap !== null ? <Meter value={pct} tone={tone} label={t("desks.agents.budget.label")} /> : null}
      <div className="hint">{t("desks.agents.budget.rule")}</div>
      {usage.state === "warn" ? (
        <p className="desk-budget-note warn" role="status">
          {t("desks.agents.budget.warn")}
        </p>
      ) : null}
      {usage.state === "exhausted" ? (
        <p className="desk-budget-note bad" role="alert">
          {t("desks.agents.budget.exhausted")}
        </p>
      ) : null}
    </div>
  );
}
