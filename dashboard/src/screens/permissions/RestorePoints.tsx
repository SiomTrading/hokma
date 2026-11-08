import { useCallback, useEffect, useState } from "react";
import { Button, ErrorState, Loading, Pane, Select } from "@/design";
import { useI18n } from "@/i18n";
import { listProjects } from "@/screens/projects/api";
import type { ProjectInfo } from "@/screens/projects/types";
import { useShell } from "@/shell/useShell";
import "./permissions.css";
import { errorCode, permissionErrorText, permissionsApi } from "./api";
import { formatWhen } from "./format";
import type { PointChanges, RestorePoint } from "./types";

function Changes({ point, data, busy, onFile, onAll }: { point: string; data: PointChanges; busy: boolean; onFile: (path: string) => void; onAll: () => void }) {
  const { t } = useI18n();
  const none = data.deleted.length === 0 && data.changed.length === 0;
  return (
    <div data-testid="point-changes" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {none ? <span className="p">{t("points.noChanges")}</span> : null}
      {data.deleted.length > 0 ? (
        <>
          <div className="perm-row-head">
            <span className="lbl">{t("points.deleted", { count: data.deleted.length })}</span>
            <Button size="sm" disabled={busy} onClick={onAll}>
              {t("points.restoreAll")}
            </Button>
          </div>
          <ul className="perm-files mono" aria-label={`${t("points.deleted", { count: data.deleted.length })} ${point}`}>
            {data.deleted.map((path) => (
              <li key={path} className="perm-row-head">
                <span style={{ overflowWrap: "anywhere" }}>{path}</span>
                <Button size="sm" disabled={busy} onClick={() => onFile(path)}>
                  {t("points.restoreFile")}
                </Button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {data.changed.length > 0 ? (
        <>
          <span className="lbl">{t("points.changed", { count: data.changed.length })}</span>
          <ul className="perm-files mono">
            {data.changed.map((path) => (
              <li key={path} className="perm-row-head">
                <span style={{ overflowWrap: "anywhere" }}>{path}</span>
                <Button size="sm" disabled={busy} onClick={() => onFile(path)}>
                  {t("points.restoreFile")}
                </Button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {data.truncated ? <span className="chat-small">{t("points.truncated")}</span> : null}
    </div>
  );
}

/** Restore points of one project: by date and conversation, with what disappeared or changed since. */
export function RestorePoints() {
  const { t, locale } = useI18n();
  const shell = useShell();
  const [projects, setProjects] = useState<ProjectInfo[] | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [points, setPoints] = useState<RestorePoint[] | null>(null);
  const [problem, setProblem] = useState<{ code: string | null; text: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [changes, setChanges] = useState<PointChanges | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    listProjects().then(
      (list) => {
        if (!alive) return;
        setProjects(list.projects);
        const first = list.projects.find((p) => p.id === list.active_id) ?? list.projects[0];
        setProjectId(first ? String(first.id) : null);
      },
      () => alive && setProjects([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async () => {
    setProblem(null);
    setOpen(null);
    setChanges(null);
    setPoints(null);
    try {
      setPoints((await permissionsApi.points(projectId)).points);
    } catch (cause) {
      setProblem({ code: errorCode(cause), text: permissionErrorText(cause) });
    }
  }, [projectId]);

  useEffect(() => {
    if (projects === null) return;
    void load();
  }, [projects, load]);

  const view = async (id: string) => {
    if (open === id) {
      setOpen(null);
      setChanges(null);
      return;
    }
    setOpen(id);
    setChanges(null);
    try {
      setChanges(await permissionsApi.changes(id, projectId));
    } catch (cause) {
      setOpen(null);
      shell.toast({ tone: "bad", text: permissionErrorText(cause) });
    }
  };

  const refreshChanges = async (id: string) => {
    try {
      setChanges(await permissionsApi.changes(id, projectId));
    } catch {
      /* the list stays as it was */
    }
  };

  const restore = async (id: string, work: () => Promise<string>) => {
    setBusy(true);
    try {
      shell.toast({ tone: "ok", text: await work() });
      await refreshChanges(id);
    } catch (cause) {
      shell.toast({ tone: "bad", text: permissionErrorText(cause) });
    } finally {
      setBusy(false);
    }
  };

  const restoreFile = (id: string, path: string) =>
    restore(id, async () => {
      await permissionsApi.restoreFile(id, path, projectId);
      return t("points.fileRestored", { path });
    });

  const restoreAll = (id: string) =>
    restore(id, async () => {
      const done = await permissionsApi.restoreDeleted(id, projectId);
      return t("points.allRestored", { count: done.count ?? done.restored.length });
    });

  let body;
  if (problem) {
    body = problem.code === "no_git" ? (
      <p className="p" role="status" data-testid="points-no-git">
        {problem.text}
      </p>
    ) : (
      <ErrorState message={problem.text} onRetry={() => void load()} />
    );
  } else if (points === null) body = <Loading />;
  else if (points.length === 0) body = <p className="p">{t("points.none")}</p>;
  else {
    body = (
      <ul className="perm-list" aria-label={t("points.list")}>
        {points.map((point) => (
          <li key={point.id}>
            <Pane className="perm-row" data-testid="restore-point">
              <div className="perm-row-head">
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span className="h2">{formatWhen(point.at, locale)}</span>
                  {point.conversation_id ? <span className="chat-small">{t("points.conversation", { id: point.conversation_id })}</span> : null}
                </div>
                <Button size="sm" onClick={() => void view(point.id)}>
                  {open === point.id ? t("points.hideChanges") : t("points.viewChanges")}
                </Button>
              </div>
              {open === point.id ? (
                changes ? (
                  <Changes point={point.id} data={changes} busy={busy} onFile={(path) => void restoreFile(point.id, path)} onAll={() => void restoreAll(point.id)} />
                ) : (
                  <Loading inline />
                )
              ) : null}
            </Pane>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <section aria-label={t("points.title")} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <span className="h2">{t("points.title")}</span>
      <p className="p" style={{ fontSize: 12 }}>
        {t("points.intro")}
      </p>
      {projects && projects.length > 1 ? (
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="lbl">{t("points.project")}</span>
          <Select value={projectId ?? ""} onChange={(e) => setProjectId(e.target.value)} aria-label={t("points.project")}>
            {projects.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </Select>
        </label>
      ) : null}
      {body}
    </section>
  );
}
