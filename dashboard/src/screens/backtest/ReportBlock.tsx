import { useEffect, useState } from "react";
import { ErrorState, Loading } from "@/design";
import { ApiError } from "@/api";
import { useI18n } from "@/i18n";
import { verdictApi } from "./api";
import { Block, Empty, isMissing } from "./Block";

/**
 * D: the report of the run (results.report_html: own SVG, no external resource, no script). It goes into an iframe with
 * sandbox="" (no allow-scripts, no allow-same-origin), so it cannot run anything even if it were not clean.
 */
export function ReportBlock({ runId }: { runId: number }) {
  const { t } = useI18n();
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setHtml(null);
    setError(null);
    verdictApi
      .report(runId)
      .then((text) => {
        if (alive) setHtml(text);
      })
      .catch((cause: unknown) => {
        if (alive) setError(cause instanceof ApiError ? cause : new ApiError(0, "unknown", String(cause)));
      });
    return () => {
      alive = false;
    };
  }, [runId, tick]);

  return (
    <Block title={t("verdict.block.report")} name="report">
      {error ? (
        isMissing(error) ? (
          <Empty>{t("verdict.report.empty")}</Empty>
        ) : (
          <ErrorState message={error.message} onRetry={() => setTick((n) => n + 1)} />
        )
      ) : html === null ? (
        <Loading inline />
      ) : (
        <iframe className="bt-report" title={t("verdict.report.title", { run: runId })} sandbox="" srcDoc={html} />
      )}
    </Block>
  );
}
