import { useState } from "react";
import { Icon, Pill, cx } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { formatElapsed, useNow } from "./clock";
import { useBackground } from "./feed";
import type { BgRow, BgState, BgTarget } from "./feed";
import "./panel.css";

const TONE: Record<BgState, "ok" | "bad" | "warn"> = { done: "ok", ok: "ok", failed: "bad", error: "bad", warning: "warn", skipped: "warn", archived: "warn", stopped: "warn" };
const STATE_KEY: Record<BgState, string> = {
  done: "panel.bg.state.done", ok: "panel.bg.state.ok", failed: "panel.bg.state.failed", error: "panel.bg.state.failed",
  warning: "panel.bg.state.warning", skipped: "panel.bg.state.skipped", archived: "panel.bg.state.archived", stopped: "panel.bg.state.stopped",
};

export function useBackgroundSummary(): string {
  const { t } = useI18n();
  const { running } = useBackground();
  return running.length > 0 ? t("panel.bg.summary.busy", { n: running.length }) : t("panel.bg.summary.idle");
}

function clockOf(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Line({ row, now, onOpen }: { row: BgRow; now: number; onOpen: (target: BgTarget) => void }) {
  const { t } = useI18n();
  const label = row.label.key ? t(row.label.key, row.label.params) : (row.label.params?.text ?? "");
  const live = row.endedAt === null;
  const body = (
    <>
      <span className={cx("pn-dot", live ? "working" : "done")} aria-hidden="true" />
      <span className="pn-main">
        <span className="pn-line">
          <span className="pn-name">{label}</span>
          {live ? <span className="pn-clock mono">{formatElapsed(now - row.startedAt)}</span> : <span className="pn-clock mono">{clockOf(row.endedAt!)}</span>}
        </span>
        {row.sub ? <span className="pn-task hint mono" title={row.sub}>{row.sub}</span> : null}
        {row.state ? <Pill tone={TONE[row.state]}>{t(STATE_KEY[row.state])}</Pill> : null}
        {row.hintKey || row.hint ? <span className="pn-hint hint">{row.hintKey ? t(row.hintKey) : row.hint}</span> : null}
      </span>
    </>
  );
  return row.target ? (
    <button type="button" className="pn-row pn-click" data-testid="bg-row" data-kind={row.kind} onClick={() => onOpen(row.target)}>
      {body}
    </button>
  ) : (
    <div className="pn-row" data-testid="bg-row" data-kind={row.kind}>
      {body}
    </div>
  );
}

export function BackgroundSection() {
  const { t } = useI18n();
  const shell = useShell();
  const { running, finished } = useBackground();
  const [showDone, setShowDone] = useState(false);
  const now = useNow(running.length > 0);
  const open = (target: BgTarget) => {
    if (!target) return;
    if (target.type === "tasks") shell.openScreen("tasks");
    else if (target.type === "backup") shell.openSettings("backup");
    else shell.openScreen("desks", { desk: String(target.id), tab: "operations" });
  };
  if (running.length === 0 && finished.length === 0) return <p className="hint pn-pad">{t("panel.bg.empty")}</p>;
  return (
    <div className="pn-list">
      <span className="sr-only" role="status">
        {t(running.length > 0 ? "panel.bg.summary.busy" : "panel.bg.summary.idle", { n: running.length })}
      </span>
      {running.length === 0 ? <p className="hint pn-pad">{t("panel.bg.noneRunning")}</p> : null}
      {running.map((row) => (
        <Line key={row.key} row={row} now={now} onOpen={open} />
      ))}
      {finished.length > 0 ? (
        <>
          <button type="button" className="pn-sub" aria-expanded={showDone} onClick={() => setShowDone((v) => !v)}>
            <Icon name={showDone ? "chevronDown" : "chevronRight"} size={12} />
            <span>{t("panel.bg.finished", { n: finished.length })}</span>
          </button>
          {showDone
            ? finished.map((row) => <Line key={row.key} row={row} now={now} onOpen={open} />)
            : null}
        </>
      ) : null}
    </div>
  );
}
