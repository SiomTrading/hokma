import { useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, ErrorState, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { Markdown } from "@/screens/learn/Markdown";
import type { NewsData, UpdateResult } from "./types";

function formatDate(iso: string, locale: string): string | null {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" });
}

/** Settings > News: installed version, the notice of a newer one with "Update Hokma", and the real notes. */
export function NewsSection() {
  const { t, locale } = useI18n();
  const [refresh, setRefresh] = useState(0);
  const news = useApiQuery<NewsData>(`/api/news?refresh=${refresh > 0}&n=${refresh}`);
  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [result, setResult] = useState<UpdateResult | null>(null);

  const update = async () => {
    setUpdating(true);
    setUpdateError(null);
    try {
      setResult(await api.post<UpdateResult>("/api/update", undefined, { timeoutMs: 0 }));
    } catch (cause) {
      setUpdateError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
    } finally {
      setUpdating(false);
    }
  };

  if (news.loading && !news.data) return <Loading />;
  if (news.error || !news.data) return <ErrorState message={news.error?.message} onRetry={news.reload} />;
  const data = news.data;
  const installedOn = data.installed.installed_at ? formatDate(data.installed.installed_at, locale) : null;
  const notes = data.newer && data.latest?.notes ? data.latest.notes : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone="acc">v{data.installed.version}</Pill>
        <Pill tone="ok">{t("news.installed")}</Pill>
        {installedOn ? <span className="mono" style={{ fontSize: 11, color: "var(--t3)" }}>{installedOn}</span> : null}
      </div>

      {result ? (
        <Pane style={{ padding: "12px 14px" }}>
          <p className="p" role="status">
            {result.status === "updated" ? t("news.updated", { version: result.to_version }) : t("news.alreadyLatest", { version: result.to_version })}
          </p>
        </Pane>
      ) : data.newer && data.latest ? (
        <Pane style={{ padding: "12px 14px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span className="p">{t("news.newer", { version: data.latest.version })}</span>
          <Button size="sm" variant="primary" disabled={updating} onClick={() => void update()}>
            {updating ? t("news.updating") : t("news.update")}
          </Button>
        </Pane>
      ) : data.latest ? (
        <Pane style={{ padding: "12px 14px" }}>
          <p className="p">{t("news.upToDate")}</p>
        </Pane>
      ) : (
        <Pane style={{ padding: "12px 14px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span className="p">{t("news.checkFailed", { message: data.latest_error ?? "" })}</span>
          <Button size="sm" onClick={() => setRefresh((n) => n + 1)}>{t("news.check")}</Button>
        </Pane>
      )}
      {updateError ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{updateError}</p> : null}

      {notes && data.latest ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="lbl">{t("news.coming")}</span>
          <div style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
            <Pill style={{ flex: "none" }}>v{data.latest.version}</Pill>
            <Markdown source={notes} />
          </div>
        </section>
      ) : null}

      {data.changelog.length > 0 ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <span className="lbl">{t("news.history")}</span>
          {data.changelog.map((section) => (
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
