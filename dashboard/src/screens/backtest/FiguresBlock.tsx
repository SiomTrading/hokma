import type { QueryState } from "@/api";
import { useI18n } from "@/i18n";
import { Block, Empty, QueryView } from "./Block";
import { formatNumber, formatShare, formatUsd, readFigures } from "./format";
import type { RunSummary } from "./types";

/** C: the saved figures of the run. Nothing is computed here: each cell is a value the engine or hokma.quant saved. */
export function FiguresBlock({ summary }: { summary: QueryState<RunSummary> }) {
  const { t, locale } = useI18n();
  const none = t("verdict.figures.none");
  return (
    <Block title={t("verdict.block.figures")} name="figures">
      <QueryView state={summary} empty={<Empty>{t("verdict.figures.empty")}</Empty>}>
        {(data) => {
          const f = readFigures(data);
          const number = (value: number | null) => formatNumber(value, locale) ?? none;
          const rows: [string, string, string?][] = [
            [t("verdict.figures.net"), formatUsd(f.net, locale) ?? none],
            [t("verdict.figures.mtm"), formatUsd(f.mtm, locale) ?? none],
            [t("verdict.figures.sharpe"), number(f.sharpe)],
            [t("verdict.figures.sortino"), number(f.sortino)],
            [t("verdict.figures.calmar"), number(f.calmar)],
            [
              t("verdict.figures.maxDrawdown"),
              formatShare(f.maxDrawdown, locale) ?? none,
              f.drawdownDays === null ? undefined : t("verdict.figures.drawdownDays", { n: f.drawdownDays.toLocaleString(locale) }),
            ],
            [t("verdict.figures.trades"), f.trades === null ? none : f.trades.toLocaleString(locale)],
            [t("verdict.figures.winRate"), formatShare(f.winRate, locale, 0) ?? none],
            [t("verdict.figures.profitFactor"), number(f.profitFactor)],
            [t("verdict.figures.ambiguous"), f.ambiguous === null ? none : f.ambiguous.toLocaleString(locale)],
          ];
          const costs: [string, number | null][] = [
            [t("verdict.figures.commission"), f.costs.commission],
            [t("verdict.figures.slippage"), f.costs.slippage],
            [t("verdict.figures.swap"), f.costs.swap],
            [t("verdict.figures.impact"), f.costs.impact],
          ];
          return (
            <>
              <dl className="bt-figures">
                {rows.map(([label, value, extra]) => (
                  <div key={label} className="bt-figure">
                    <dt className="lbl">{label}</dt>
                    <dd>
                      <b>{value}</b>
                      {extra ? <span className="bt-figure-extra"> ({extra})</span> : null}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="p bt-costs">
                <span className="lbl">{t("verdict.figures.costs")}</span>{" "}
                {costs.map(([label, value]) => `${label} ${formatUsd(value, locale) ?? none}`).join(" | ")}
              </p>
            </>
          );
        }}
      </QueryView>
    </Block>
  );
}
