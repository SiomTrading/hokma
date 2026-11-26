import { useI18n } from "@/i18n";
import { Block, Empty } from "./Block";
import { NOTICE_CODES, splitCode } from "./format";
import type { MonteCarloData, RunSummary, Verdict, WalkForwardData } from "./types";

/** Every notice code that is saved with the verdict and its runs, once each, in the order they were found. */
export function collectNotices(input: {
  verdict: Verdict;
  summary?: RunSummary;
  walkforward?: WalkForwardData;
  montecarlo?: MonteCarloData;
}): string[] {
  const found: string[] = [];
  const add = (code: string) => {
    if (code.trim() !== "" && !found.includes(code)) found.push(code);
  };
  (input.verdict.notices ?? []).forEach(add);
  (input.summary?.warnings ?? []).forEach(add);
  if (input.summary && input.summary.ambiguous_bars > 0) add(`ambiguous_bars:${input.summary.ambiguous_bars}`);
  (input.walkforward?.warnings ?? []).forEach(add);
  (input.montecarlo?.warnings ?? []).forEach(add);
  if (input.verdict.runs.montecarlo === null && !input.montecarlo) add("monte_carlo_missing");
  return found;
}

/** I: the notices in plain language. A code this screen does not know is shown as "Technical notice" with the original text. */
export function NoticesBlock({ codes }: { codes: string[] }) {
  const { t } = useI18n();
  return (
    <Block title={t("verdict.block.notices")} name="notices">
      {codes.length === 0 ? (
        <Empty>{t("verdict.notice.empty")}</Empty>
      ) : (
        <ul className="bt-notices">
          {codes.map((raw) => {
            const { code, arg } = splitCode(raw);
            const known = (NOTICE_CODES as readonly string[]).includes(code);
            return (
              <li key={raw} className="bt-notice" data-known={known ? "yes" : "no"}>
                {known ? (
                  <>
                    <span className="p">{t(`verdict.notice.${code}`, { n: arg, part: arg, param: arg })}</span>
                    <span className="bt-notice-code">{t("verdict.notice.technical", { code: raw })}</span>
                  </>
                ) : (
                  <span className="p">{t("verdict.notice.unknown", { code: raw })}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Block>
  );
}
