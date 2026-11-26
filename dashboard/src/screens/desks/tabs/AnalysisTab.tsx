import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, ErrorState, Loading, Select } from "@/design";
import { useI18n } from "@/i18n";
import { analysisApi, applyStep } from "../analysisApi";
import type { AnalysisEvent, AnalysisList, AnalysisReport } from "../analysisApi";
import { watchAnalysis } from "../analysisFeed";
import { AnalysisReportView } from "../AnalysisReportView";
import { deskConfigApi, failText } from "../configApi";
import { DeskEmptyState } from "../EmptyState";
import { loadFocusAsset, saveFocusAsset } from "../terminalPrefs";
import type { TabProps } from "./types";

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; value: T };

/**
 * Analysis of a desk (SPEC-028, delivery 4): pick an asset of the desk's list, press Analyze and watch the eight steps go by as the app pushes them
 * (no polling), then read the report, ask about it and decide. A run that the chief opened from the chat shows here too: live when the panel is
 * open, from the list when it was not. A desk that is not a stock options desk shows the reason instead of the controls.
 */
export function AnalysisTab({ desk }: TabProps) {
  const { t } = useI18n();
  const [list, setList] = useState<Load<AnalysisList>>({ state: "loading" });
  const [assets, setAssets] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [runId, setRunId] = useState<number | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const selected = useRef<number | null>(null);
  const picker = useRef<HTMLDivElement>(null);
  selected.current = runId;

  const openRun = useCallback(
    (id: number) => {
      selected.current = id;
      setRunId(id);
      setReportError(null);
      analysisApi.get(desk.id, id).then(
        (body) => {
          if (selected.current === id) setReport(body);
        },
        (cause) => {
          if (selected.current === id) setReportError(failText(cause, t("desks.analysis.reportError")));
        },
      );
    },
    [desk.id, t],
  );

  const readList = useCallback(
    (open: boolean) => {
      analysisApi.list(desk.id).then(
        (body) => {
          setList({ state: "ready", value: body });
          if (open && selected.current === null && body.analyses.length > 0) {
            openRun((body.analyses.find((a) => a.status === "running") ?? body.analyses[0]).run_id);
          }
        },
        (cause) => setList({ state: "error", message: failText(cause, t("desks.analysis.error")) }),
      );
    },
    [desk.id, openRun, t],
  );

  useEffect(() => {
    setList({ state: "loading" });
    selected.current = null;
    setRunId(null);
    setReport(null);
    readList(true);
    deskConfigApi.detail(desk.id).then(
      (detail) => {
        setAssets(detail.assets);
        const saved = loadFocusAsset(desk.id);
        setSymbol(saved && detail.assets.includes(saved) ? saved : (detail.assets[0] ?? ""));
      },
      () => setAssets([]),
    );
  }, [desk.id, readList]);

  const onEvent = useCallback(
    (event: AnalysisEvent) => {
      if (event.event === "started") {
        openRun(event.run_id);
        readList(false);
        return;
      }
      if (event.event === "step") {
        if (event.run_id === selected.current) setReport((now) => (now ? { ...now, steps: applyStep(now.steps, event) } : now));
        return;
      }
      readList(false);
      if (event.run_id === selected.current) openRun(event.run_id);
    },
    [openRun, readList],
  );

  // The socket opened again: what the run pushed meanwhile never arrived, so the list and the report on the screen are read once more.
  const reread = useCallback(() => {
    readList(false);
    if (selected.current !== null) openRun(selected.current);
  }, [readList, openRun]);

  useEffect(() => watchAnalysis(desk.id, onEvent, reread), [desk.id, onEvent, reread]);

  const analyze = () => {
    if (starting || !symbol) return;
    setStarting(true);
    setNotice(null);
    analysisApi
      .start(desk.id, symbol)
      .then((out) => {
        openRun(out.run_id);
        readList(false);
      })
      .catch((cause) => {
        if (cause instanceof ApiError && cause.code === "analysis_running" && typeof cause.data.run_id === "number") {
          setNotice(t("desks.analysis.alreadyRunning", { symbol }));
          openRun(cause.data.run_id);
          return;
        }
        if (cause instanceof ApiError && cause.code === "analysis_unavailable") readList(false);
        setNotice(failText(cause, t("desks.analysis.startFailed")));
      })
      .finally(() => setStarting(false));
  };

  if (list.state === "loading") return <Loading label={t("desks.analysis.loading")} />;
  if (list.state === "error") return <ErrorState message={list.message} onRetry={() => readList(true)} />;
  const found = list.value;
  if (!found.available) {
    return <DeskEmptyState icon="chart" title={t("desks.analysis.unavailable.title")} text={found.message ?? t("desks.analysis.unavailable.text")} />;
  }
  const running = report?.status === "running";

  return (
    <section className="analysis-tab" aria-label={t("desks.tab.analysis")}>
      {assets.length === 0 ? (
        <DeskEmptyState icon="chart" title={t("desks.analysis.noAssets.title")} text={t("desks.analysis.noAssets.text")} />
      ) : (
        <div className="analysis-pick" ref={picker}>
          <span className="lbl">{t("desks.analysis.asset")}</span>
          <Select
            aria-label={t("desks.analysis.asset")}
            value={symbol}
            onChange={(event) => {
              setSymbol(event.target.value);
              saveFocusAsset(desk.id, event.target.value);
            }}
          >
            {assets.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </Select>
          <Button variant="primary" onClick={analyze} disabled={starting || !symbol}>
            {starting ? t("desks.analysis.starting") : t("desks.analysis.analyze")}
          </Button>
        </div>
      )}
      {notice ? (
        <p className="analysis-note" role="alert">
          {notice}
        </p>
      ) : null}
      {reportError ? <ErrorState message={reportError} onRetry={() => runId !== null && openRun(runId)} /> : null}
      {report && !reportError ? (
        <AnalysisReportView
          key={report.run_id}
          desk={desk}
          report={report}
          live={running}
          onChanged={() => runId !== null && openRun(runId)}
          onAnalyzeOther={() => picker.current?.querySelector("select")?.focus()}
        />
      ) : null}
      {!report && !reportError && found.analyses.length === 0 ? (
        <DeskEmptyState icon="chart" title={t("desks.analysis.empty.title")} text={t("desks.analysis.empty.text")} />
      ) : null}
      {found.analyses.length > 0 ? (
        <section className="analysis-prev" aria-label={t("desks.analysis.previous")}>
          <h3>{t("desks.analysis.previous")}</h3>
          <ul>
            {found.analyses.map((item) => (
              <li key={item.run_id}>
                <button type="button" className="analysis-prev-row" aria-current={item.run_id === runId ? "true" : undefined} onClick={() => openRun(item.run_id)}>
                  <b>{item.symbol ?? "-"}</b>
                  <span>{item.status === "running" ? t("desks.analysis.state.running") : (item.rating ?? t(`desks.analysis.status.${item.status}`))}</span>
                  <time className="mono">{(item.started_at ?? "").slice(0, 16).replace("T", " ")}</time>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
