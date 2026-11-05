import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, useApiQuery } from "@/api";
import { Button, ErrorState, Field, Input, Loading, Meter, Pane, Pill, Row } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { usageApi } from "./api";
import { formatDate, formatTokens } from "./format";
import type { UsageData, UsageRow } from "./types";

/** Settings > Models and usage: this week's tokens against the cap the owner sets, and where they went. */
export function ModelsSettings() {
  const { t, locale } = useI18n();
  const shell = useShell();
  const query = useApiQuery<UsageData>("/api/usage/weekly");
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cap = query.data?.cap ?? null;

  useEffect(() => setText(cap ? String(cap) : ""), [cap]);

  if (query.loading && !query.data) return <Loading />;
  if (query.error || !query.data) return <ErrorState message={query.error?.message} onRetry={query.reload} />;
  const data = query.data;

  const save = async (value: number | null) => {
    setBusy(true);
    setError(null);
    try {
      await usageApi.setCap(value);
      shell.toast({ tone: "ok", text: t(value === null ? "models.cap.cleared" : "models.cap.saved") });
      shell.emit("usage:changed");
      query.reload();
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = Number(text.replace(/[\s.,_]/g, ""));
    if (!/^\d+$/.test(text.replace(/[\s.,_]/g, "")) || !Number.isSafeInteger(value) || value < 1) {
      setError(t("models.cap.invalid"));
      return;
    }
    void save(value);
  };

  const pct = data.pct;
  const tone = pct !== null && pct >= 90 ? "bad" : pct !== null && pct >= 70 ? "warn" : "acc";
  const used = formatTokens(data.used, locale);

  return (
    <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <section style={{ display: "flex", flexDirection: "column", gap: 8 }} aria-label={t("models.title")}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
          <span className="h2">{t("models.title")}</span>
          <span className="mono" style={{ color: pct !== null && pct >= 100 ? "var(--bad)" : "var(--t2)" }}>
            {pct !== null ? t("models.pct", { pct: Math.round(pct) }) : "-"}
          </span>
        </div>
        <Meter value={pct ?? 0} tone={tone} large label={t("models.title")} />
        <span className="p" style={{ fontSize: 12 }}>
          {data.cap
            ? t("models.tokens", { used, cap: formatTokens(data.cap, locale) })
            : t("models.tokens.nocap", { used })}
        </span>
        {pct !== null && pct >= 100 ? <span className="p" role="alert" style={{ color: "var(--bad)" }}>{t("models.over")}</span> : null}
        {data.cap === null ? <span className="p" role="note">{t("models.unset")}</span> : null}
        <span className="p" style={{ fontSize: 12 }}>{t("models.note")}</span>
        <span className="p" style={{ fontSize: 12, color: "var(--t3)" }}>
          {t("models.week", { from: formatDate(data.starts_at, locale), to: formatDate(data.ends_at, locale) })}
        </span>
      </section>

      <form onSubmit={submit} style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }} noValidate>
        <div style={{ flex: "1 1 220px" }}>
          <Field label={t("models.cap.label")} hint={t("models.cap.hint")} error={error ?? undefined}>
            {(control) => (
              <Input {...control} inputMode="numeric" value={text} placeholder="500000" onChange={(e) => { setText(e.target.value); setError(null); }} />
            )}
          </Field>
        </div>
        <Button variant="primary" type="submit" disabled={busy || !data.profile_exists}>{t("models.cap.save")}</Button>
        {data.cap !== null ? <Button variant="ghost" disabled={busy} onClick={() => void save(null)}>{t("models.cap.clear")}</Button> : null}
      </form>

      <Breakdown title={t("models.breakdown.model")} rows={data.by_model} label={(key) => (key === "haiku" || key === "sonnet" || key === "opus" ? t(`agents.model.${key}`) : key)} />
      <Breakdown title={t("models.breakdown.agent")} rows={data.by_agent} label={(key) => key} />

      <Pane style={{ padding: "4px 0" }}>
        <Row>
          <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 3 }}>
            <span className="h2">{t("models.claude")}</span>
            <span className="p" style={{ fontSize: 12 }}>
              {data.claude.plan ? t("models.claude.plan", { plan: data.claude.plan }) : t("models.claude.desc")}
            </span>
          </div>
          <Pill tone={data.claude.status === "connected" ? "ok" : "warn"}>{t(`models.claude.${data.claude.status}`)}</Pill>
        </Row>
      </Pane>
    </div>
  );
}

function Breakdown({ title, rows, label }: { title: string; rows: UsageRow[]; label: (key: string) => string }) {
  const { t, locale } = useI18n();
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 6 }} aria-label={title}>
      <span className="lbl">{title}</span>
      {rows.length === 0 ? (
        <span className="p" style={{ fontSize: 12 }}>{t("models.breakdown.empty")}</span>
      ) : (
        <Pane style={{ padding: "4px 0" }}>
          {rows.map((row) => (
            <Row key={row.key}>
              <span className="h2" style={{ flex: 1 }}>{label(row.key)}</span>
              <span className="mono" style={{ color: "var(--t2)" }}>
                {t("models.breakdown.row", { tokens: formatTokens(row.tokens_in + row.tokens_out, locale) })}
              </span>
            </Row>
          ))}
        </Pane>
      )}
    </section>
  );
}
