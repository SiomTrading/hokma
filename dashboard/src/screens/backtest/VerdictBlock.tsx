import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { Block } from "./Block";
import type { Verdict } from "./types";

interface Props {
  verdict: Verdict;
  ownerBlocked: boolean;
  /** Opens the questions sheet. */
  onAnswer: () => void;
  /** True while the run has no walk-forward: the first sentence says so. */
  noWalkForward: boolean;
}

/** A: the suggestion in one color, three sentences and the way to answer what is open. The word "Suggestion" is always there. */
export function VerdictBlock({ verdict, ownerBlocked, onAnswer, noWalkForward }: Props) {
  const { t } = useI18n();
  const open = verdict.open_questions.length;
  return (
    <Block title={t("verdict.block.verdict")} name="verdict" className="bt-verdict">
      <div className="bt-verdict-box" data-variant={verdict.verdict} role="status">
        <p className="bt-verdict-title">
          <span className="bt-verdict-dot" aria-hidden="true" />
          {t(`verdict.variant.${verdict.verdict}`)}
        </p>
        {noWalkForward ? <p className="p bt-verdict-warning">{t("verdict.screen.noWalkForward")}</p> : null}
        <ol className="bt-sentences">
          {verdict.sentences.map((sentence, index) => (
            <li key={index} className="p">
              {sentence}
            </li>
          ))}
        </ol>
        <p className="p bt-advisory">{t("verdict.advisory")}</p>
        {verdict.audit_verdict !== verdict.verdict ? (
          <p className="p bt-raw">{t("verdict.rawVerdict", { variant: t(`verdict.variant.${verdict.audit_verdict}`) })}</p>
        ) : null}
        {open > 0 ? (
          <p className="bt-open-questions">
            <span className="p">{open === 1 ? t("verdict.questions.open.one") : t("verdict.questions.open.many", { n: open })}</span>
            <Button size="sm" onClick={onAnswer} disabled={ownerBlocked}>
              {t("verdict.questions.answer")}
            </Button>
          </p>
        ) : null}
      </div>
    </Block>
  );
}
