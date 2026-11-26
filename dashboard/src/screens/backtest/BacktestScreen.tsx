import { useEffect, useState } from "react";
import { useApiQuery } from "@/api";
import { Button, EmptyState, ErrorState, Loading, Select } from "@/design";
import { useI18n } from "@/i18n";
import type { ScreenProps } from "@/shell/registry";
import { ChecksBlock } from "./ChecksBlock";
import { FiguresBlock } from "./FiguresBlock";
import { MonteCarloBlock } from "./MonteCarloBlock";
import { NoticesBlock, collectNotices } from "./NoticesBlock";
import { OptimizationBlock } from "./OptimizationBlock";
import { QuestionsSheet } from "./QuestionsSheet";
import { ReportBlock } from "./ReportBlock";
import { SuggestionsBlock } from "./SuggestionsBlock";
import { CopyThresholdsNotice, ThresholdsPanel, lineHasNoThresholds } from "./ThresholdsPanel";
import { VerdictBlock } from "./VerdictBlock";
import { WalkForwardBlock } from "./WalkForwardBlock";
import { errorText, hasCode, jobErrorText, paths, useOwnerBlocked, verdictApi } from "./api";
import { formatDateTime } from "./format";
import type { LineId, MonteCarloData, RunSummary, StrategyRow, ThresholdSet, Verdict, WalkForwardData } from "./types";
import "./backtest.css";

/** How often the screen asks whether a new evaluation finished (ms). */
export const POLL_MS = 2000;

interface Selection {
  strategy: string;
  version: number;
}

function fromParams(params: Record<string, string>): Selection | null {
  const version = Number(params.version);
  return params.strategy && Number.isInteger(version) && version >= 1 ? { strategy: params.strategy, version } : null;
}

const keyOf = (s: Selection) => `${s.strategy}@${s.version}`;

function parseKey(value: string): Selection | null {
  const at = value.lastIndexOf("@");
  if (at < 1) return null;
  const version = Number(value.slice(at + 1));
  return Number.isInteger(version) && version >= 1 ? { strategy: value.slice(0, at), version } : null;
}

/** The Backtest screen (SPEC-032 7.2): header, the nine blocks, the thresholds editor and the questions sheet. */
export function BacktestScreen({ params }: ScreenProps) {
  const { t, locale } = useI18n();
  const ownerBlocked = useOwnerBlocked();
  const strategies = useApiQuery<StrategyRow[]>(paths.strategies);
  const [sel, setSel] = useState<Selection | null>(() => fromParams(params));
  const [local, setLocal] = useState<Verdict | null>(null);
  const [thresholdsOpen, setThresholdsOpen] = useState(false);
  const [questionsOpen, setQuestionsOpen] = useState(false);
  const [evaluating, setEvaluating] = useState<{ baseId: number | null; jobId: number | null } | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [evalError, setEvalError] = useState<{ text: string; noRun: boolean } | null>(null);

  const latest = useApiQuery<Verdict>(sel ? paths.latest(sel.strategy, sel.version) : null);
  const verdict = local && sel && local.strategy_id === sel.strategy && local.strategy_version === sel.version ? local : (latest.data ?? null);

  useEffect(() => setLocal(null), [latest.data, sel]);
  useEffect(() => {
    setEvaluating(null);
    setProgress(null);
    setEvalError(null);
    setQuestionsOpen(false);
  }, [sel]);

  const row = sel ? strategies.data?.find((r) => r.strategy_id === sel.strategy && r.version === sel.version) : undefined;
  const line: LineId | null = verdict?.line ?? row?.line ?? null;
  const thresholds = useApiQuery<ThresholdSet>(line ? paths.thresholds(line) : null);
  const noThresholds = line !== null && lineHasNoThresholds(thresholds);

  const summary = useApiQuery<RunSummary>(verdict ? paths.summary(verdict.runs.run) : null);
  const walk = useApiQuery<WalkForwardData>(verdict?.runs.walkforward ? paths.walkforward(verdict.runs.walkforward) : null);
  const carlo = useApiQuery<MonteCarloData>(verdict?.runs.montecarlo ? paths.montecarlo(verdict.runs.montecarlo) : null);

  // The progress the server reports while it evaluates, and the verdict that arrives when it is done.
  const serverProgress = verdict?.evaluating ?? null;
  useEffect(() => {
    if (serverProgress && !evaluating && verdict) setEvaluating({ baseId: verdict.verdict_id, jobId: null });
  }, [serverProgress, evaluating, verdict]);

  useEffect(() => {
    if (!evaluating || !sel) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let jobId = evaluating.jobId;
    const tick = async () => {
      let jobDone = false;
      let fromJob = false;
      if (jobId !== null) {
        try {
          const job = await verdictApi.job(jobId);
          if (!alive) return;
          if (job.state === "failed") {
            // The evaluation failed: say why and stop waiting.
            setEvaluating(null);
            setProgress(null);
            setEvalError({ text: job.error ? jobErrorText(job.error, t) : t("verdict.error.generic"), noRun: false });
            return;
          }
          jobDone = job.state === "done";
          fromJob = true;
          if (!jobDone) setProgress({ done: job.done, total: job.total });
        } catch (cause) {
          if (!alive) return;
          if (hasCode(cause, "no_job")) jobId = null; // the engine restarted: the verdict itself says when it is done
        }
      }
      try {
        const next = await verdictApi.latest(sel.strategy, sel.version);
        if (!alive) return;
        if ((jobDone || next.verdict_id !== evaluating.baseId) && !next.evaluating) {
          setEvaluating(null);
          setProgress(null);
          setLocal(next);
          latest.reload();
          return;
        }
        if (!fromJob) setProgress(next.evaluating ?? null);
      } catch {
        /* nothing saved yet or a short failure: ask again */
      }
      if (alive) timer = setTimeout(() => void tick(), POLL_MS);
    };
    timer = setTimeout(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evaluating, sel]);

  const evaluate = async () => {
    if (!sel) return;
    setEvalError(null);
    try {
      const started = await verdictApi.evaluate(sel.strategy, sel.version);
      setEvaluating({ baseId: verdict?.verdict_id ?? null, jobId: typeof started?.job_id === "number" ? started.job_id : null });
    } catch (cause) {
      setEvalError({ text: errorText(cause, t("verdict.error.generic")), noRun: hasCode(cause, "no_run") });
    }
  };

  const list = strategies.data ?? [];
  const known = sel ? list.some((r) => r.strategy_id === sel.strategy && r.version === sel.version) : true;
  const removed = verdict?.excluded.filter((e) => e.by !== "system").length ?? 0;       // the ones the owner took out, not the ones that do not apply
  const noRun = (latest.error !== undefined && hasCode(latest.error, "no_run")) || evalError?.noRun === true;
  const neverEvaluated = !verdict && latest.error !== undefined && !noRun && latest.error.status === 404;
  const running = evaluating !== null;
  const shownProgress = progress ?? serverProgress;

  const meta = verdict
    ? [
        t("verdict.header.last", { when: formatDateTime(verdict.created_at, locale) }),
        t("verdict.header.run", { run: verdict.runs.run }),
        t("verdict.header.thresholds", { line: verdict.thresholds.line, version: verdict.thresholds.version }),
        removed === 0 ? null : removed === 1 ? t("verdict.header.removed.one") : t("verdict.header.removed.many", { n: removed }),
      ]
        .filter(Boolean)
        .join(" | ")
    : "";

  return (
    <section className="bt-screen" aria-label={t("verdict.screen.title")}>
      <header className="bt-header">
        <div className="bt-header-row">
          <h1 className="h2 bt-title">{t("verdict.screen.title")}</h1>
          <label className="bt-picker">
            <span className="bt-sr">{t("verdict.select.label")}</span>
            <Select
              aria-label={t("verdict.select.label")}
              value={sel ? keyOf(sel) : ""}
              onChange={(event) => {
                setSel(parseKey(event.target.value));
                setThresholdsOpen(false);
              }}
            >
              <option value="">{t("verdict.select.placeholder")}</option>
              {sel && !known ? <option value={keyOf(sel)}>{t("verdict.select.option", { name: sel.strategy, version: sel.version, line: line ?? "" })}</option> : null}
              {list.map((r) => (
                <option key={keyOf({ strategy: r.strategy_id, version: r.version })} value={keyOf({ strategy: r.strategy_id, version: r.version })}>
                  {t("verdict.select.option", { name: r.strategy_id, version: r.version, line: r.line })}
                </option>
              ))}
            </Select>
          </label>
          <Button onClick={() => void evaluate()} disabled={!sel || running || noRun}>
            {t("verdict.evaluate")}
          </Button>
          <Button onClick={() => setThresholdsOpen((open) => !open)} disabled={line === null} aria-expanded={thresholdsOpen}>
            {t("verdict.thresholds.open", { line: line ?? "" })}
          </Button>
        </div>
        {meta ? <p className="p bt-meta">{meta}</p> : null}
        {removed > 0 && verdict ? <p className="p bt-inherited">{removed === 1 ? t("verdict.header.inherited.one") : t("verdict.header.inherited", { n: removed })}</p> : null}
      </header>

      {ownerBlocked ? (
        <p className="bt-owner-warning" role="status">
          {t("verdict.owner.missing")}
        </p>
      ) : null}

      {running ? (
        <div className="bt-progress" role="status">
          <span className="p">
            {shownProgress ? (shownProgress.total > 0 ? t("verdict.screen.progress", { k: shownProgress.done, n: shownProgress.total }) : t("verdict.screen.preparing")) : t("verdict.screen.evaluating")}
          </span>
          {shownProgress && shownProgress.total > 0 ? <progress max={shownProgress.total} value={shownProgress.done} aria-label={t("verdict.screen.evaluating")} /> : null}
          {verdict ? <span className="p">{t("verdict.screen.evaluatingKeep")}</span> : null}
        </div>
      ) : null}

      {line !== null && noThresholds ? <CopyThresholdsNotice line={line} ownerBlocked={ownerBlocked} onDone={thresholds.reload} /> : null}

      {thresholdsOpen && line !== null ? (
        <ThresholdsPanel
          line={line}
          current={thresholds}
          verdict={verdict}
          ownerBlocked={ownerBlocked}
          onChanged={thresholds.reload}
          onRecomputed={setLocal}
          onClose={() => setThresholdsOpen(false)}
        />
      ) : null}

      <div className="bt-body">
        {strategies.loading && !strategies.data && !sel ? <Loading /> : null}
        {strategies.error && !sel ? <ErrorState message={strategies.error.message} onRetry={strategies.reload} /> : null}
        {!sel && strategies.data ? (
          list.length === 0 ? (
            <EmptyState icon="backtest" title={t("verdict.screen.noStrategies")} />
          ) : (
            <EmptyState icon="backtest" title={t("verdict.screen.pick")} />
          )
        ) : null}

        {sel && !verdict && latest.loading ? <Loading /> : null}
        {sel && !verdict && noRun ? <EmptyState icon="backtest" title={t("verdict.screen.noRun")} /> : null}
        {sel && neverEvaluated ? (
          <EmptyState
            icon="backtest"
            title={t("verdict.screen.noVerdict")}
            action={
              <Button variant="primary" onClick={() => void evaluate()} disabled={running}>
                {t("verdict.evaluate.first")}
              </Button>
            }
          />
        ) : null}
        {sel && !verdict && latest.error && !noRun && !neverEvaluated ? <ErrorState message={latest.error.message} onRetry={latest.reload} /> : null}
        {evalError && !evalError.noRun ? (
          <p className="err" role="alert">
            {evalError.text}
          </p>
        ) : null}

        {sel && verdict ? (
          <>
            <VerdictBlock verdict={verdict} ownerBlocked={ownerBlocked} onAnswer={() => setQuestionsOpen(true)} noWalkForward={verdict.runs.walkforward === null} />
            {questionsOpen ? (
              <QuestionsSheet
                strategy={sel.strategy}
                version={sel.version}
                verdict={verdict}
                ownerBlocked={ownerBlocked}
                onVerdict={setLocal}
                onClose={() => setQuestionsOpen(false)}
              />
            ) : null}
            <ChecksBlock verdict={verdict} ownerBlocked={ownerBlocked} onVerdict={setLocal} onAnswer={() => setQuestionsOpen(true)} />
            <FiguresBlock summary={summary} />
            <ReportBlock runId={verdict.runs.run} />
            <OptimizationBlock optId={verdict.runs.optimize} ideal={verdict.ideal} />
            <WalkForwardBlock query={verdict.runs.walkforward ? walk : null} />
            <MonteCarloBlock runId={verdict.runs.run} query={verdict.runs.montecarlo ? carlo : null} ownerBlocked={ownerBlocked} />
            <SuggestionsBlock suggestions={verdict.suggestions} />
            <NoticesBlock codes={collectNotices({ verdict, summary: summary.data, walkforward: walk.data, montecarlo: carlo.data })} />
          </>
        ) : null}
      </div>
    </section>
  );
}
