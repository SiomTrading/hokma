import type { QueryState } from "@/api";
import { useI18n } from "@/i18n";
import { Block, Empty, QueryView } from "./Block";
import { describeParams, formatDate, formatNumber } from "./format";
import type { WalkForwardData, WalkForwardWindow } from "./types";

function range(
  dates: [string, string] | undefined,
  rows: [number, number],
  locale: string,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (dates) return `${formatDate(dates[0], locale)} - ${formatDate(dates[1], locale)}`;
  return t("verdict.wf.rows", { from: rows[0].toLocaleString(locale), to: rows[1].toLocaleString(locale) });
}

function testRange(window: WalkForwardWindow, locale: string, t: (key: string, params?: Record<string, string | number>) => string): string {
  return window.test_blocks
    .map((block, index) => range(window.test_dates?.[index], block, locale, t))
    .join("; ");
}

/** F: the windows of the walk-forward as the server saved them (WalkForwardResult.to_dict). */
export function WalkForwardBlock({ query }: { query: QueryState<WalkForwardData> | null }) {
  const { t, locale } = useI18n();
  const empty = <Empty>{t("verdict.wf.empty")}</Empty>;
  return (
    <Block title={t("verdict.block.walkforward")} name="walkforward">
      {query === null ? (
        empty
      ) : (
        <QueryView state={query} empty={empty}>
          {(data) => (
            <>
              <p className="p bt-wf-summary">
                {[
                  t("verdict.wf.summary", { scheme: t(`verdict.wf.scheme.${data.scheme}`), n: data.windows.length }),
                  data.efficiency === null ? null : t("verdict.wf.efficiency", { value: formatNumber(data.efficiency, locale) ?? "" }),
                  data.oos_total === null ? null : t("verdict.wf.oosTotal", { value: formatNumber(data.oos_total, locale) ?? "" }),
                  data.leakage_free ? t("verdict.wf.noLeak") : t("verdict.wf.leak"),
                ]
                  .filter(Boolean)
                  .join(" | ")}
              </p>
              <div className="bt-table-wrap">
                <table className="bt-table">
                  <caption className="bt-sr">{t("verdict.wf.caption")}</caption>
                  <thead>
                    <tr>
                      <th scope="col">{t("verdict.wf.window")}</th>
                      <th scope="col">{t("verdict.wf.train")}</th>
                      <th scope="col">{t("verdict.wf.test")}</th>
                      <th scope="col">{t("verdict.wf.chosen")}</th>
                      <th scope="col">{t("verdict.wf.inSample")}</th>
                      <th scope="col">{t("verdict.wf.outSample")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.windows.map((window) => (
                      <tr key={window.index}>
                        <th scope="row">{window.index + 1}</th>
                        <td>{range(window.train_dates, window.train, locale, t)}</td>
                        <td>{testRange(window, locale, t)}</td>
                        <td>{describeParams(window.chosen, locale)}</td>
                        <td className="bt-num">{formatNumber(window.in_sample, locale) ?? ""}</td>
                        <td className="bt-num">{formatNumber(window.out_of_sample, locale) ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {data.warnings.length > 0 ? <p className="p bt-warnings">{data.warnings.join(", ")}</p> : null}
            </>
          )}
        </QueryView>
      )}
    </Block>
  );
}
