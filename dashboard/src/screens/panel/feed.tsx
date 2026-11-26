import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/api";
import { usePanelState } from "@/shell/panelState";
import { parseTime } from "../chat/activity";
import { useActivity } from "../chat/useActivity";
import type { BackupState } from "../settings/BackupSection";
import type { JobsData } from "../agents/types";

const DAY_MS = 24 * 3600 * 1000;
const FAST_MS = 5000;
const SLOW_MS = 60_000;

export type BgTarget = { type: "tasks" } | { type: "backup" } | { type: "desk"; id: number } | null;
export type BgState = "done" | "failed" | "skipped" | "archived" | "ok" | "warning" | "error" | "stopped";

/** One line of "In the background", whatever its source. */
export interface BgRow {
  key: string;
  kind: "routine" | "backup" | "desk" | "shell";
  /** The text key and its parameters; the row is translated where it is drawn. */
  label: { key: string; params?: Record<string, string> };
  sub?: string;
  startedAt: number;
  endedAt: number | null;
  state?: BgState;
  hint?: string;
  /** A fixed text instead of `hint`, by key. */
  hintKey?: string;
  target: BgTarget;
}

export interface Feed {
  running: BgRow[];
  finished: BgRow[];
}

interface DeskRuns {
  running: { run_id: number; desk_id: number; desk_name: string; kind: string; started_at: string }[];
  finished_24h: { run_id: number; desk_id: number; desk_name: string; kind: string; status: string; ended_at: string }[];
}

const FeedContext = createContext<Feed>({ running: [], finished: [] });
export const useBackground = (): Feed => useContext(FeedContext);

function useVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

/** GET `path` every `ms`; the first read is immediate and a new period reads again. */
function usePolled<T>(path: string, ms: number): T | undefined {
  const [data, setData] = useState<T>();
  useEffect(() => {
    let alive = true;
    const read = () => {
      api.get<T>(path).then(
        (value) => alive && setData(value),
        () => undefined,
      );
    };
    read();
    const timer = setInterval(read, ms);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [path, ms]);
  return data;
}

function firstLines(text: string | undefined, lines = 2): string | undefined {
  const out = (text ?? "").split("\n").map((line) => line.trim()).filter(Boolean).slice(0, lines).join("\n");
  return out || undefined;
}

/** Joins the sources of "In the background" and polls them: every 5 s while the section is in front, every 60 s otherwise. */
export function BackgroundFeed({ open, children }: { open: boolean; children: ReactNode }) {
  const visible = useVisible();
  const ms = open && visible ? FAST_MS : SLOW_MS;
  const jobs = usePolled<JobsData>("/api/scheduled-jobs", ms);
  const backup = usePolled<BackupState>("/api/backup", ms);
  const runs = usePolled<DeskRuns>("/api/runs/active", ms);
  const { followed } = usePanelState();
  const conv = useActivity(followed?.conversationId ?? null);
  const seen = useRef(new Map<string, number>());
  const now = Date.now();
  const since = (key: string): number => {
    if (!seen.current.has(key)) seen.current.set(key, Date.now());
    return seen.current.get(key)!;
  };

  const running: BgRow[] = [];
  const finished: BgRow[] = [];
  const runningIds = new Set(jobs?.running ?? []);
  for (const job of jobs?.jobs ?? []) {
    if (runningIds.has(job.id)) {
      const started = parseTime(job.last?.started_at);
      running.push({ key: `job-${job.id}`, kind: "routine", label: { key: "", params: { text: job.name } }, sub: job.agent_name, startedAt: started || since(`job-${job.id}`), endedAt: null, target: { type: "tasks" } });
    } else {
      const ended = parseTime(job.last?.ended_at);
      if (!job.last || !ended || now - ended > DAY_MS) continue;
      const state: BgState = job.last.status === "done" || job.last.status === "completed" ? "done" : job.last.status === "failed" || job.last.status === "error" ? "failed" : job.last.status === "archived" ? "archived" : "skipped";
      finished.push({ key: `job-${job.id}-${job.last.task_id}`, kind: "routine", label: { key: "", params: { text: job.name } }, sub: job.agent_name, startedAt: parseTime(job.last.started_at) || ended, endedAt: ended, state, hint: firstLines(job.last.summary), target: { type: "tasks" } });
    }
  }
  if (backup?.status === "running") {
    running.push({ key: "backup", kind: "backup", label: { key: "panel.bg.backup" }, startedAt: since("backup"), endedAt: null, target: { type: "backup" } });
  } else if (backup?.last_backup_at && now - parseTime(backup.last_backup_at) <= DAY_MS) {
    const state: BgState = backup.status === "warning" ? "warning" : backup.status === "error" ? "error" : "ok";
    const at = parseTime(backup.last_backup_at);
    finished.push({ key: `backup-${backup.last_backup_at}`, kind: "backup", label: { key: "panel.bg.backup" }, startedAt: at, endedAt: at, state, hint: backup.message ?? undefined, target: { type: "backup" } });
  }
  for (const run of runs?.running ?? []) {
    running.push({ key: `run-${run.run_id}`, kind: "desk", label: { key: "panel.bg.deskRun", params: { desk: run.desk_name } }, startedAt: parseTime(run.started_at), endedAt: null, target: { type: "desk", id: run.desk_id } });
  }
  for (const run of runs?.finished_24h ?? []) {
    const at = parseTime(run.ended_at);
    finished.push({ key: `run-${run.run_id}`, kind: "desk", label: { key: "panel.bg.deskRun", params: { desk: run.desk_name } }, startedAt: at, endedAt: at, state: run.status === "done" ? "done" : "failed", target: { type: "desk", id: run.desk_id } });
  }
  for (const turn of conv.turns) {
    for (const shell of turn.shells) {
      const row: BgRow = { key: `shell-${shell.id}`, kind: "shell", label: { key: "panel.bg.shell" }, sub: shell.command, startedAt: shell.startedAt, endedAt: shell.endedAt, target: null };
      if (shell.status === "running") running.push(row);
      // A command still open when its turn ends is closed as "stopped" by the engine, which does not know how it really ended.
      else finished.push({ ...row, state: shell.status === "done" ? "done" : shell.status === "error" ? "failed" : "stopped", ...(shell.status === "stopped" ? { hintKey: "panel.bg.shellEnded" } : {}) });
    }
  }
  finished.sort((a, b) => (b.endedAt ?? 0) - (a.endedAt ?? 0));
  return <FeedContext.Provider value={{ running, finished }}>{children}</FeedContext.Provider>;
}
