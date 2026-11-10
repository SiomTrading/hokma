import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, justUpdated, watchEngine } from "@/api";
import { Button, ErrorState, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { Markdown } from "@/screens/learn/Markdown";
import type { NewsData, UpdateStage } from "./types";
import { refreshUpdateStatus, startUpdate, useUpdateStatus } from "./updateStatus";

const STAGES: UpdateStage[] = ["download", "unpack", "python", "dependencies", "verify", "switch"];
const PANE_ROW = { padding: "12px 14px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" } as const;

function formatDate(iso: string, locale: string): string | null {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" });
}

function clock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}

function messageOf(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/**
 * Settings > News. "Check for updates" is always on screen. States: up to date, checking, a newer version,
 * updating (real stages with a live clock), restarting (the page reloads by itself) and error (with "Retry").
 */
export function NewsSection() {
  const { t, locale } = useI18n();
  const status = useUpdateStatus();
  const [news, setNews] = useState<NewsData | null>(null);
  const [checking, setChecking] = useState(true);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<"restarting" | "manual" | null>(null);
  const startedAt = useRef<number | null>(null);
  const now = useNow(1000);

  const check = useCallback(async (refresh: boolean) => {
    setChecking(true);
    setCheckError(null);
    try {
      const data = await api.get<NewsData>(refresh ? "/api/news?refresh=true" : "/api/news", { timeoutMs: 60_000 });
      setNews(data);
      if (data.latest === null) setCheckError(data.latest_error ?? "");
      void refreshUpdateStatus();
    } catch (cause) {
      setCheckError(messageOf(cause, t("api.error")));
    } finally {
      setChecking(false);
    }
  }, [t]);

  useEffect(() => {
    void check(false);
    void refreshUpdateStatus();
  }, [check]);

  const serverUpdating = status?.phase === "updating";
  const isUpdating = updating || serverUpdating;
  const restarting = outcome === "restarting" || status?.phase === "restarting";

  // The page was reloaded or reopened while an update runs: pick the clock up from the engine.
  useEffect(() => {
    if (serverUpdating && startedAt.current === null) startedAt.current = Date.now() - (status?.elapsed_s ?? 0) * 1000;
    if (!serverUpdating && !updating) startedAt.current = null;
  }, [serverUpdating, updating, status?.elapsed_s]);

  // While it updates, ask the engine for the real stage every second.
  useEffect(() => {
    if (!isUpdating) return;
    const id = setInterval(() => void refreshUpdateStatus(), 1000);
    return () => clearInterval(id);
  }, [isUpdating]);

  // The engine announced its own restart (an update started elsewhere): follow it.
  useEffect(() => {
    if (status?.phase === "restarting") watchEngine({ mode: "update", expectedVersion: status.target_version });
    if (status?.phase === "restart_required") setOutcome("manual");
  }, [status?.phase, status?.target_version]);

  const update = async () => {
    setUpdating(true);
    setUpdateError(null);
    setOutcome(null);
    startedAt.current = Date.now();
    try {
      const result = await startUpdate();
      if (result.status === "launched") {
        setOutcome("restarting");                     // startUpdate() already watches the engine
      } else if (result.status !== "updated") {
        void check(true);
      } else if (result.restarting) {
        setOutcome("restarting");
        watchEngine({ mode: "restart", expectedVersion: result.to_version });
      } else {
        setOutcome("manual");
      }
    } catch (cause) {
      setUpdateError(messageOf(cause, t("api.error")));
    } finally {
      setUpdating(false);
      void refreshUpdateStatus();
    }
  };

  if (!news && checking) return <Loading label={t("news.checking")} />;
  if (!news) return <ErrorState message={checkError ?? undefined} onRetry={() => void check(true)} />;

  const checkedAt = news.checked_at ?? status?.checked_at ?? null;
  const when = (() => {
    const ms = checkedAt ? now - Date.parse(checkedAt) : Number.NaN;
    if (Number.isNaN(ms)) return t("news.neverChecked");
    const minutes = Math.floor(Math.max(0, ms) / 60000);
    if (minutes < 1) return t("news.checked", { when: t("news.ago.now") });
    if (minutes < 60) return t("news.checked", { when: t("news.ago.min", { n: minutes }) });
    if (minutes < 60 * 24) return t("news.checked", { when: t("news.ago.hours", { n: Math.floor(minutes / 60) }) });
    return t("news.checked", { when: t("news.ago.days", { n: Math.floor(minutes / 1440) }) });
  })();
  const installedOn = news.installed.installed_at ? formatDate(news.installed.installed_at, locale) : null;
  const notes = news.newer && news.latest?.notes ? news.latest.notes : null;
  const error = updateError;
  const busy = checking || isUpdating || restarting;
  const currentStage = status?.stage ? STAGES.indexOf(status.stage) : -1;
  const elapsed = startedAt.current === null ? (status?.elapsed_s ?? 0) : (now - startedAt.current) / 1000;

  const checkButton = (
    <Button size="sm" disabled={busy} onClick={() => void check(true)}>
      {checking ? t("news.checking") : t("news.check")}
    </Button>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone="acc">v{news.installed.version}</Pill>
        <Pill tone="ok">{t("news.installed")}</Pill>
        {installedOn ? <span className="mono" style={{ fontSize: 11, color: "var(--t3)" }}>{installedOn}</span> : null}
      </div>

      {justUpdated ? (
        <Pane style={{ padding: "12px 14px" }}>
          <p className="p" role="status">{t("news.updatedTo", { version: justUpdated })}</p>
        </Pane>
      ) : null}

      {restarting ? (
        <Pane style={PANE_ROW}>
          <span className="p" role="status" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span className="spin" aria-hidden="true" />
            {t("news.restarting")}
          </span>
          {checkButton}
        </Pane>
      ) : isUpdating ? (
        <Pane style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span className="p" role="status" style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <span className="spin" aria-hidden="true" />
              {t("news.updatingTitle")}
            </span>
            <span className="mono" style={{ fontSize: 12, color: "var(--t3)" }} data-testid="update-clock">
              {t("news.elapsed", { time: clock(elapsed) })}
            </span>
          </div>
          <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            {STAGES.map((stage, index) => {
              const state = index < currentStage ? "done" : index === currentStage ? "now" : "next";
              return (
                <li key={stage} data-state={state} className="p" style={{ color: state === "next" ? "var(--t3)" : "var(--t1)", display: "flex", gap: 8 }}>
                  <span aria-hidden="true" style={{ width: 14 }}>{state === "done" ? "✓" : state === "now" ? "›" : "·"}</span>
                  {t(`news.stage.${stage}`)}
                </li>
              );
            })}
          </ol>
          <div>{checkButton}</div>
        </Pane>
      ) : error !== null ? (
        <Pane style={PANE_ROW}>
          <span className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</span>
          <span style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void update()}>{t("news.retry")}</Button>
            {checkButton}
          </span>
        </Pane>
      ) : outcome === "manual" ? (
        <Pane style={PANE_ROW}>
          <span className="p" role="status">{t("news.restartManual")}</span>
          {checkButton}
        </Pane>
      ) : checkError !== null ? (
        <Pane style={PANE_ROW}>
          <span className="p" role="alert">{t("news.checkFailed", { message: checkError })}</span>
          <span style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void check(true)}>{t("news.retry")}</Button>
            {checkButton}
          </span>
        </Pane>
      ) : news.newer && news.latest ? (
        <Pane style={PANE_ROW}>
          <span className="p">{t("news.newer", { version: news.latest.version })}</span>
          <span style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void update()}>{t("news.update")}</Button>
            {checkButton}
          </span>
        </Pane>
      ) : (
        <Pane style={PANE_ROW}>
          <span className="p">
            {t("news.upToDate", { version: news.installed.version })} · {when}
          </span>
          {checkButton}
        </Pane>
      )}

      {notes && news.latest && !isUpdating && !restarting ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="lbl">{t("news.coming")}</span>
          <div style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
            <Pill style={{ flex: "none" }}>v{news.latest.version}</Pill>
            <Markdown source={notes} />
          </div>
        </section>
      ) : null}

      {news.changelog.length > 0 ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <span className="lbl">{t("news.history")}</span>
          {news.changelog.map((section) => (
            <div key={section.version} style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
              <Pill style={{ flex: "none" }}>v{section.version}</Pill>
              <Markdown source={section.body} />
            </div>
          ))}
        </section>
      ) : null}
    </div>
  );
}
