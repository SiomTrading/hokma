import { useEffect, useState } from "react";
import { ApiError, useApiQuery } from "@/api";
import { Button, EmptyState, ErrorState, Loading, Modal, Pane, Pill, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import type { ScreenProps } from "@/shell/registry";
import { useShell } from "@/shell/useShell";
import { jobsApi } from "./api";
import { describeSchedule, formatDateTime, formatTokens } from "./format";
import { JobForm } from "./JobForm";
import type { AgentItem, AgentsData, JobItem, JobsData } from "./types";

const REFRESH_MS = 3000;

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** Scheduled tasks: the list with its on/off switches, the form, and the confirmation that turns one on. */
export function TasksScreen(_props: ScreenProps) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const jobs = useApiQuery<JobsData>("/api/scheduled-jobs");
  const agents = useApiQuery<AgentsData>("/api/agents");
  const [editing, setEditing] = useState<JobItem | "new" | null>(null);
  const [confirming, setConfirming] = useState<JobItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { reload } = jobs;

  // A run starts and ends without the owner doing anything: keep the list and the last result current.
  useEffect(() => {
    const timer = setInterval(reload, REFRESH_MS);
    return () => clearInterval(timer);
  }, [reload]);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(false);
    }
  };

  const activate = (job: JobItem) =>
    run(async () => {
      await jobsApi.activate(job.id);
      shell.toast({ tone: "ok", text: t("tasks.activated") });
      setConfirming(null);
      jobs.reload();
    });

  const deactivate = (job: JobItem) =>
    run(async () => {
      await jobsApi.deactivate(job.id);
      shell.toast({ tone: "info", text: t("tasks.deactivated") });
      jobs.reload();
    });

  const remove = (job: JobItem) => {
    if (!window.confirm(t("tasks.delete.confirm", { name: job.name }))) return;
    void run(async () => {
      await jobsApi.remove(job.id);
      shell.toast({ tone: "info", text: t("tasks.deleted") });
      jobs.reload();
    });
  };

  if ((jobs.loading && !jobs.data) || (agents.loading && !agents.data)) return <Loading />;
  if (jobs.error || !jobs.data) return <ErrorState message={jobs.error?.message} onRetry={jobs.reload} />;
  const choices: AgentItem[] = [...(agents.data?.orchestrator ? [agents.data.orchestrator] : []), ...(agents.data?.agents ?? [])];

  return (
    <section className="fade" style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8, overflow: "auto" }}>
      <Pane style={{ padding: "14px 16px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span className="p" style={{ flex: 1, minWidth: 220, color: "var(--t1)" }}>{t("tasks.notice")}</span>
        <Button variant="primary" onClick={() => setEditing("new")}>{t("tasks.new")}</Button>
      </Pane>
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}

      {jobs.data.jobs.length === 0 ? (
        <Pane>
          <EmptyState icon="tasks" title={t("tasks.empty.title")} description={t("tasks.empty.body")} />
        </Pane>
      ) : (
        <Pane style={{ padding: "4px 0" }}>
          {jobs.data.jobs.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              running={jobs.data?.running.includes(job.id) ?? false}
              locale={locale}
              disabled={busy}
              onToggle={(on) => (on ? setConfirming(job) : void deactivate(job))}
              onEdit={() => setEditing(job)}
              onDelete={() => remove(job)}
            />
          ))}
        </Pane>
      )}

      {editing ? (
        <JobForm
          job={editing === "new" ? null : editing}
          agents={choices}
          onClose={() => setEditing(null)}
          onSaved={(saved, wasActive) => {
            setEditing(null);
            jobs.reload();
            shell.toast({ tone: wasActive && !saved.active ? "warn" : "ok", text: t(wasActive && !saved.active ? "tasks.form.edited.off" : "tasks.form.created") });
          }}
        />
      ) : null}

      {confirming ? (
        <Modal label={t("tasks.confirm.title", { name: confirming.name })} onClose={() => setConfirming(null)} style={{ width: "min(480px, 94vw)" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 22 }}>
            <h2 className="h1">{t("tasks.confirm.title", { name: confirming.name })}</h2>
            <p className="p">
              {t("tasks.confirm.body", {
                agent: confirming.agent_name,
                model: confirming.model ? t(`agents.model.${confirming.model}`) : "",
              })}
            </p>
            <p className="p" style={{ fontSize: 12 }}>{describeSchedule(confirming, t, locale)}</p>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <Button variant="ghost" onClick={() => setConfirming(null)}>{t("tasks.confirm.no")}</Button>
              <Button variant="primary" disabled={busy} onClick={() => void activate(confirming)}>{t("tasks.confirm.yes")}</Button>
            </div>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}

interface RowProps {
  job: JobItem;
  running: boolean;
  locale: string;
  disabled: boolean;
  onToggle: (on: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
}

function JobRow({ job, running, locale, disabled, onToggle, onEdit, onDelete }: RowProps) {
  const { t } = useI18n();
  const last = job.last;
  const statusKey = last ? `tasks.status.${last.status}` : "";
  const known = ["done", "failed", "running", "review", "awaiting_approval"].includes(last?.status ?? "");
  return (
    <Row style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
      <div style={{ flex: "1 1 260px", display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
        <span className="h2">{job.name}</span>
        <span className="p" style={{ fontSize: 12 }}>{describeSchedule(job, t, locale)}</span>
        <span className="p" style={{ fontSize: 12, color: "var(--t3)" }}>
          {running
            ? t("tasks.running")
            : !job.agent_active
              ? t("tasks.agentOff")
              : job.active && job.next_run_at
                ? t("tasks.next", { date: formatDateTime(job.next_run_at, locale) })
                : t("tasks.off")}
        </span>
        {last ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span className="p" style={{ fontSize: 12 }}>
              {t("tasks.last", { status: known ? t(statusKey) : t("tasks.status.other", { status: last.status }) })}
              {last.tokens ? ` · ${t("tasks.last.tokens", { tokens: formatTokens(last.tokens, locale) })}` : ""}
            </span>
            {last.summary || last.error ? (
              <span className="p" style={{ fontSize: 12, color: last.status === "failed" ? "var(--bad)" : "var(--t2)", whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 96, overflow: "auto" }}>
                {last.status === "failed" ? last.error || last.summary : last.summary}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <Pill tone="acc">{job.agent_name}</Pill>
      {job.model ? <Pill>{t(`agents.model.${job.model}`)}</Pill> : null}
      <Toggle checked={job.active} label={t("tasks.toggle", { name: job.name })} disabled={disabled || running} onChange={onToggle} />
      <span style={{ display: "flex", gap: 4 }}>
        <Button size="sm" variant="ghost" disabled={disabled || running} onClick={onEdit}>{t("tasks.edit")}</Button>
        <Button size="sm" variant="ghost" disabled={disabled || running} onClick={onDelete}>{t("tasks.delete")}</Button>
      </span>
    </Row>
  );
}
