import { useState } from "react";
import { Button, Input, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { errorText, verdictApi } from "./api";
import { Block } from "./Block";
import { formatDate, phaseKey, splitCode } from "./format";
import { PHASES } from "./types";
import type { CheckStatus, Verdict, VerdictCheck, VerdictExclusion } from "./types";

/** The check of the traffic light that each question of the sheet answers (section 5.3). */
export const QUESTION_OF_CHECK: Record<string, string> = {
  point_in_time: "point_in_time",
  survivorship_free: "survivorship_free",
  corporate_actions: "corporate_actions",
  data_gaps: "gaps_checked",
  in_sample_tuning: "in_sample_tuning",
  holdout_untouched: "holdout_untouched",
  impact_modeled: "impact_modeled",
  financing_and_borrow: "borrow_costs_modeled",
  capacity: "capacity_checked",
};

const TONE: Record<CheckStatus, "ok" | "warn" | "bad" | "default"> = { pass: "ok", warn: "warn", fail: "bad", unknown: "default", na: "default" };

interface Props {
  verdict: Verdict;
  ownerBlocked: boolean;
  onVerdict: (verdict: Verdict) => void;
  onAnswer: () => void;
}

/** B: the 24 checks grouped by the six phases of audit.PHASES, with Remove, Put back and Answer. */
export function ChecksBlock({ verdict, ownerBlocked, onVerdict, onAnswer }: Props) {
  const { t, locale } = useI18n();
  const [removing, setRemoving] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const exclusionOf = (key: string): VerdictExclusion | undefined => verdict.excluded.find((item) => item.key === key);
  const openKeys = new Set(verdict.open_questions.map((q) => q.key));

  const run = async (key: string, call: () => Promise<Verdict>) => {
    setBusy(key);
    setError(null);
    try {
      onVerdict(await call());
      setRemoving(null);
      setReason("");
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setBusy(null);
    }
  };

  const name = (check: VerdictCheck) => {
    const text = t(`verdict.check.${check.key}`);
    return text === `verdict.check.${check.key}` ? check.key : text;
  };

  const statusText = (check: VerdictCheck) => {
    const own = `verdict.checkstate.${check.key}.${check.status}`;
    const { code } = splitCode(check.note);
    const variants = /^[a-z_]+:/.test(check.note) ? [`${own}.${code}`] : [];
    for (const key of [...variants, own]) {
      const text = t(key);
      if (text !== key) return text;
    }
    return t(`verdict.checkstate.generic.${check.status}`);
  };

  return (
    <Block title={t("verdict.block.checks")} name="checks">
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
      {PHASES.map((phase) => {
        const items = verdict.checks.filter((check) => check.phase === phase).sort((a, b) => a.number - b.number);
        if (items.length === 0) return null;
        return (
          <section key={phase} className="bt-phase" aria-label={t(phaseKey(phase))}>
            <h3 className="lbl">{t(phaseKey(phase))}</h3>
            <ul className="bt-checks">
              {items.map((check) => {
                const exclusion = check.excluded ? exclusionOf(check.key) : undefined;
                const excluded = check.excluded || exclusion !== undefined;
                // What the system took out because it does not apply cannot be put back from the screen (the route refuses it): the answer of the question decides.
                const automatic = exclusion?.by === "system";
                const question = QUESTION_OF_CHECK[check.key];
                const asks = check.status === "unknown" && !excluded && question !== undefined && openKeys.has(question);
                return (
                  <li key={check.key} className="bt-check" data-status={excluded ? "excluded" : check.status} data-check={check.key}>
                    <span className="bt-check-no" aria-label={t("verdict.row.number", { n: check.number })}>
                      {check.number}
                    </span>
                    <Pill tone={excluded ? "default" : TONE[check.status]}>
                      {excluded ? (automatic ? t("verdict.status.na") : t("verdict.status.excluded")) : t(`verdict.status.${check.status}`)}
                    </Pill>
                    <div className="bt-check-main">
                      <span className={excluded ? "bt-check-name bt-struck" : "bt-check-name"}>{name(check)}</span>
                      <span className={excluded ? "p bt-struck" : "p"}>{statusText(check)}</span>
                      {excluded ? (
                        <span className="p bt-excluded-info">
                          {exclusion?.reason === "owner_said_na"
                            ? t("verdict.row.excludedNa")
                            : automatic
                              ? t("verdict.row.excludedAuto")
                              : [
                                t("verdict.row.excludedOn", { date: exclusion ? formatDate(exclusion.at, locale) : "" }),
                                exclusion?.reason ? t("verdict.row.excludedReason", { reason: exclusion.reason }) : "",
                              ]
                                .filter(Boolean)
                                .join(". ")}
                        </span>
                      ) : null}
                      {check.note ? (
                        <details className="bt-detail">
                          <summary>{t("verdict.row.detail")}</summary>
                          <code>{check.note}</code>
                        </details>
                      ) : null}
                      {removing === check.key ? (
                        <form
                          className="bt-remove-form"
                          onSubmit={(event) => {
                            event.preventDefault();
                            void run(check.key, () => verdictApi.exclude(verdict.verdict_id, [check.key], reason.trim() || undefined));
                          }}
                        >
                          <label className="lbl" htmlFor={`bt-reason-${check.key}`}>
                            {t("verdict.row.reasonLabel")}
                          </label>
                          <Input id={`bt-reason-${check.key}`} value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} />
                          <Button size="sm" variant="primary" type="submit" disabled={ownerBlocked || busy !== null}>
                            {t("verdict.row.confirmExclude")}
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => setRemoving(null)}>
                            {t("verdict.row.cancel")}
                          </Button>
                        </form>
                      ) : null}
                    </div>
                    <div className="bt-check-actions">
                      {asks ? (
                        <Button size="sm" onClick={onAnswer} disabled={ownerBlocked} aria-label={`${t("verdict.row.answer")}: ${name(check)}`}>
                          {t("verdict.row.answer")}
                        </Button>
                      ) : null}
                      {excluded && !automatic ? (
                        <Button
                          size="sm"
                          onClick={() => void run(check.key, () => verdictApi.include(verdict.verdict_id, [check.key]))}
                          disabled={ownerBlocked || busy !== null}
                          aria-label={`${t("verdict.row.include")}: ${name(check)}`}
                        >
                          {t("verdict.row.include")}
                        </Button>
                      ) : null}
                      {!excluded && check.status !== "na" ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setRemoving(check.key);
                            setReason("");
                          }}
                          disabled={ownerBlocked || busy !== null}
                          aria-label={`${t("verdict.row.exclude")}: ${name(check)}`}
                        >
                          {t("verdict.row.exclude")}
                        </Button>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </Block>
  );
}
