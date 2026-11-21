import { useId, useState } from "react";
import { Icon } from "@/design";
import { useI18n } from "@/i18n";

const MAX_STEPS = 20;

/** The guide of a provider (4.5): what it gives, what it costs, the steps and what to do if the test fails. Only the parts that exist. */
function useGuide(id: string) {
  const { t } = useI18n();
  const text = (key: string): string | null => {
    const value = t(key);
    return value === key ? null : value;
  };
  const steps: string[] = [];
  if (id === "mail") {
    // Gmail keeps the text it always had: how to make the app password.
    const help = text("mail.help");
    if (help) steps.push(help);
  } else {
    for (let n = 1; n <= MAX_STEPS; n += 1) {
      const step = text("connections.guide." + id + ".steps." + n);
      if (!step) break;
      steps.push(step);
    }
  }
  return { what: text("connections.guide." + id + ".what"), cost: text("connections.guide." + id + ".cost"), steps, fail: text("connections.guide." + id + ".fail") };
}

/** "If the test fails" of a provider: under the guide, and under the error when a test or a sign-in fails. */
export function GuideFail({ id }: { id: string }) {
  const { t } = useI18n();
  const { fail } = useGuide(id);
  if (!fail) return null;
  return (
    <p className="conn-guide-fail p" data-testid="guide-fail">
      <strong>{t("connections.guide.fail")}: </strong>
      {fail}
    </p>
  );
}

interface Props {
  id: string;
  /** A fold that opens and closes (forms). Without it the guide is always shown (the "View guide" dialog). */
  foldable?: boolean;
  /** The fold starts open. */
  open?: boolean;
}

/** Block "How to get the details". */
export function ProviderGuide({ id, foldable = false, open = true }: Props) {
  const { t } = useI18n();
  const [shown, setShown] = useState(open);
  const bodyId = useId();
  const { what, cost, steps, fail } = useGuide(id);
  if (!what && !cost && steps.length === 0 && !fail) return null;
  const expanded = !foldable || shown;

  return (
    <section className="conn-guide" aria-label={t("connections.guide.title")} data-testid="provider-guide" data-provider={id}>
      {foldable ? (
        <button type="button" className="conn-guide-fold" aria-expanded={shown} aria-controls={bodyId} onClick={() => setShown((value) => !value)}>
          <Icon name={shown ? "chevronDown" : "chevronRight"} size={14} />
          <span className="lbl">{t("connections.guide.title")}</span>
        </button>
      ) : (
        <h3 className="lbl">{t("connections.guide.title")}</h3>
      )}
      <div id={bodyId} className="conn-guide-body" hidden={!expanded}>
        {what ? (
          <p className="p" data-testid="guide-what">
            <strong>{t("connections.guide.what")}: </strong>
            {what}
          </p>
        ) : null}
        {cost ? (
          <p className="p" data-testid="guide-cost">
            <strong>{t("connections.guide.cost")}: </strong>
            {cost}
          </p>
        ) : null}
        {steps.length > 0 ? (
          <div data-testid="guide-steps">
            <strong className="p">{t("connections.guide.steps")}:</strong>
            <ol className="conn-guide-steps p">
              {steps.map((step, index) => (
                <li key={index}>{step}</li>
              ))}
            </ol>
          </div>
        ) : null}
        {fail ? (
          <p className="p" data-testid="guide-fail-block">
            <strong>{t("connections.guide.fail")}: </strong>
            {fail}
          </p>
        ) : null}
      </div>
    </section>
  );
}
