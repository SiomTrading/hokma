import { useEffect, useState } from "react";
import { useApiQuery } from "@/api";
import { Button, ErrorState, Loading, Pane, Textarea } from "@/design";
import { useI18n } from "@/i18n";
import { errorText, paths, verdictApi } from "./api";
import type { AnswerInput, AnswerValue, QuestionState, Verdict } from "./types";

interface Props {
  strategy: string;
  version: number;
  verdict: Verdict;
  ownerBlocked: boolean;
  /** The verdict after the answers were saved and the suggestion recomputed. */
  onVerdict: (verdict: Verdict) => void;
  onClose: () => void;
}

const OPTIONS: { value: AnswerValue; label: string }[] = [
  { value: "yes", label: "verdict.q.yes" },
  { value: "no", label: "verdict.q.no" },
  { value: "na", label: "verdict.q.na" },
];

/** 7.4: the questions the system cannot answer alone. Yes, No or Not applicable, an optional note, and the suggestion of each. */
export function QuestionsSheet({ strategy, version, verdict, ownerBlocked, onVerdict, onClose }: Props) {
  const { t } = useI18n();
  const states = useApiQuery<QuestionState[]>(paths.questions(strategy, version));
  const [chosen, setChosen] = useState<Record<string, AnswerValue>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [reopened, setReopened] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const own = `${strategy}@${version}`;
  const all = states.data ?? [];
  const isData = (q: QuestionState) => (q.scope ? q.scope === "data" : q.subject !== own);
  const asking = all.filter((q) => q.status === "ask" || reopened.includes(q.key));
  const answered = all.filter((q) => q.status === "answered" && !reopened.includes(q.key));

  useEffect(() => {
    setChosen({});
    setNotes({});
    setReopened([]);
  }, [strategy, version]);

  const send = async (inputs: AnswerInput[]) => {
    if (inputs.length === 0) {
      setError(t("verdict.q.nothing"));
      return;
    }
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await verdictApi.answers(strategy, version, inputs);
      setRecomputing(true);
      onVerdict(await verdictApi.recompute(verdict.verdict_id, "answers"));
      setChosen({});
      setNotes({});
      setReopened([]);
      setSaved(true);
      states.reload();
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setBusy(false);
      setRecomputing(false);
    }
  };

  const withNote = (key: string, value: AnswerValue): AnswerInput => {
    const note = (notes[key] ?? "").trim();
    return note ? { key, value, note } : { key, value };
  };

  const saveChosen = () => send(asking.filter((q) => chosen[q.key] !== undefined).map((q) => withNote(q.key, chosen[q.key] as AnswerValue)));
  /** Answers only the questions that come with a suggestion, with that suggestion. The others stay open. */
  const confirmSuggestions = () => send(asking.filter((q) => q.suggestion !== null).map((q) => withNote(q.key, q.suggestion as AnswerValue)));
  const anySuggestion = asking.some((q) => q.suggestion !== null);

  const label = (value: AnswerValue) => t(OPTIONS.find((o) => o.value === value)?.label ?? "verdict.q.na");

  return (
    <Pane as="section" className="bt-block bt-questions" aria-label={t("verdict.q.title", { n: asking.length })}>
      <header className="bt-block-head">
        <h2 className="h2">{t("verdict.q.title", { n: asking.length })}</h2>
        <Button size="sm" variant="ghost" onClick={onClose}>
          {t("verdict.q.close")}
        </Button>
      </header>
      <div className="bt-block-body">
        {states.loading && !states.data ? <Loading inline /> : null}
        {states.error ? <ErrorState message={states.error.message} onRetry={states.reload} /> : null}
        {states.data && asking.length === 0 ? <p className="p">{t("verdict.q.none")}</p> : null}
        {asking.map((q, index) => {
          const text = t(`verdict.question.${q.key}`);
          return (
            <fieldset key={q.key} className="bt-question" data-question={q.key} disabled={ownerBlocked || busy}>
              <legend className="p">
                {index + 1}. {text}
              </legend>
              <div className="bt-question-options" role="radiogroup" aria-label={t("verdict.q.group", { question: text })}>
                {OPTIONS.map((option) => (
                  <label key={option.value} className="bt-radio">
                    <input
                      type="radio"
                      name={`bt-q-${q.key}`}
                      value={option.value}
                      checked={chosen[q.key] === option.value}
                      onChange={() => setChosen((prev) => ({ ...prev, [q.key]: option.value }))}
                    />{" "}
                    {t(option.label)}
                  </label>
                ))}
              </div>
              {q.suggestion ? (
                <p className="p bt-suggested" title={q.suggestion_reason ?? undefined}>
                  {t("verdict.q.suggested", { answer: label(q.suggestion) })}
                </p>
              ) : null}
              {isData(q) ? <p className="p">{t("verdict.q.scopeData", { subject: q.subject })}</p> : null}
              <label className="lbl" htmlFor={`bt-note-${q.key}`}>
                {t("verdict.q.note")}
              </label>
              <Textarea
                id={`bt-note-${q.key}`}
                rows={1}
                maxLength={1000}
                value={notes[q.key] ?? ""}
                onChange={(event) => setNotes((prev) => ({ ...prev, [q.key]: event.target.value }))}
              />
            </fieldset>
          );
        })}
        {answered.length > 0 ? (
          <section className="bt-answered" aria-label={t("verdict.q.answered")}>
            <h3 className="lbl">{t("verdict.q.answered")}</h3>
            <ul>
              {answered.map((q) => (
                <li key={q.key} className="p">
                  {t(`verdict.question.${q.key}`)}: <b>{q.answer ? label(q.answer) : ""}</b>
                  {isData(q) ? ` (${q.subject})` : ""}{" "}
                  <Button size="sm" variant="ghost" disabled={ownerBlocked || busy} onClick={() => setReopened((prev) => [...prev, q.key])}>
                    {t("verdict.q.change")}
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {recomputing ? (
          <p className="p bt-ok" role="status">
            {t("verdict.recomputing")}
          </p>
        ) : null}
        {error ? (
          <p className="err" role="alert">
            {error}
          </p>
        ) : null}
        {saved ? (
          <p className="p bt-ok" role="status">
            {t("verdict.q.saved")}
          </p>
        ) : null}
        <div className="bt-save-row">
          <Button onClick={() => void confirmSuggestions()} disabled={ownerBlocked || busy || !anySuggestion}>
            {t("verdict.q.confirm")}
          </Button>
          <Button variant="primary" onClick={() => void saveChosen()} disabled={ownerBlocked || busy || Object.keys(chosen).length === 0}>
            {t("verdict.q.save")}
          </Button>
        </div>
      </div>
    </Pane>
  );
}
