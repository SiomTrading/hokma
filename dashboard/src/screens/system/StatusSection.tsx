import { useState } from "react";
import { useApiQuery } from "@/api";
import { Button, Dot, ErrorState, Loading, Pane, Row } from "@/design";
import { useI18n } from "@/i18n";
import type { CheckStatus, StatusData } from "./types";

const TONE: Record<CheckStatus, { color: string; pill: string }> = {
  ok: { color: "var(--ok)", pill: "pill ok" },
  warn: { color: "var(--warn)", pill: "pill warn" },
  fail: { color: "var(--bad)", pill: "pill bad" },
};

/** Settings > System status: the checks behind `hokma doctor`, with their detail, and "Check again". */
export function StatusSection() {
  const { t } = useI18n();
  const [round, setRound] = useState(0);
  const status = useApiQuery<StatusData>(`/api/status?n=${round}`);

  if (status.loading && !status.data) return <Loading />;
  if (status.error || !status.data) return <ErrorState message={status.error?.message} onRetry={status.reload} />;
  const data = status.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
          <span className="p mono" style={{ fontSize: 12 }}>{t("sys.version")}: {data.version}</span>
          <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{t("sys.workdir")}: {data.workdir}</span>
          <span className="p" style={{ fontSize: 12 }}>{t("sys.summary", { ok: data.summary.ok, warn: data.summary.warn, fail: data.summary.fail })}</span>
        </div>
        <Button size="sm" disabled={status.loading} onClick={() => setRound((n) => n + 1)}>{t("sys.recheck")}</Button>
      </div>
      <Pane style={{ padding: "4px 0" }} aria-label={t("sys.checks")} as="section">
        {data.checks.map((check) => (
          <Row key={check.name} style={{ alignItems: "flex-start" }}>
            <span className={TONE[check.status].pill} style={{ flex: "none" }}>
              <Dot color={TONE[check.status].color} />
              {t(`sys.state.${check.status}`)}
            </span>
            <span style={{ width: 190, flex: "none", font: "400 13px Montserrat, sans-serif" }}>{check.name}</span>
            <span className="p" style={{ fontSize: 12, flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>{check.detail}</span>
          </Row>
        ))}
      </Pane>
    </div>
  );
}
