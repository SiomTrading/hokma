import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, EmptyState, ErrorState, Icon, Input, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import * as client from "./foldersApi";
import type { DiskFolder, FolderStatus } from "./foldersApi";

const STATUS_TONE: Record<FolderStatus, "ok" | "acc" | "bad"> = {
  ready: "ok",
  scanning: "acc",
  missing: "bad",
  failed: "bad",
};

/** Name of a folder for a title: the last part of its path (the full path is shown below it). */
function lastSegment(path: string): string {
  const parts = path.split(/[/\\]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** "Disk folders" tab of a project: the folders the agent reads, with status and counts, rescan and remove. */
export function FoldersSection({ projectId }: { projectId: number }) {
  const { t } = useI18n();
  const shell = useShell();
  const [folders, setFolders] = useState<DiskFolder[] | null>(null);
  const [max, setMax] = useState(5);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState<number | "add" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const data = await client.listFolders(projectId);
      if (!alive.current) return;
      setFolders(data.folders);
      setMax(data.max);
      setLoadError(false);
    } catch {
      if (alive.current) setLoadError(true);
    }
  }, [projectId]);

  useEffect(() => {
    setFolders(null);
    setLoadError(false);
    setProblem(null);
    setTyping(false);
    setTyped("");
    void load();
  }, [load]);

  const messageOf = useCallback(
    (cause: unknown): string => (cause instanceof ApiError && cause.message ? cause.message : t("folders.error.generic")),
    [t],
  );

  const add = async (path: string) => {
    setBusy("add");
    setProblem(null);
    try {
      await client.addFolder(projectId, path);
      if (!alive.current) return;
      setTyping(false);
      setTyped("");
      shell.toast({ tone: "ok", text: t("folders.toast.added") });
      await load();
    } catch (cause) {
      if (alive.current) setProblem(messageOf(cause));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const pick = async () => {
    setBusy("add");
    setProblem(null);
    try {
      const result = await client.pickFolder();
      if (!alive.current) return;
      if (!result.available) {
        setTyping(true);
        setBusy(null);
        return;
      }
      if (result.path) {
        await add(result.path);
        return;
      }
      setBusy(null);
    } catch (cause) {
      if (alive.current) {
        setProblem(messageOf(cause));
        setBusy(null);
      }
    }
  };

  const rescan = async (folder: DiskFolder) => {
    setBusy(folder.id);
    setProblem(null);
    try {
      await client.rescanFolder(projectId, folder.id);
      if (!alive.current) return;
      await load();
    } catch (cause) {
      if (alive.current) setProblem(messageOf(cause));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const remove = async (folder: DiskFolder) => {
    setBusy(folder.id);
    setProblem(null);
    try {
      await client.removeFolder(projectId, folder.id);
      if (!alive.current) return;
      shell.toast({ tone: "info", text: t("folders.toast.removed") });
      await load();
    } catch (cause) {
      if (alive.current) setProblem(messageOf(cause));
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const others = folders ? folders.filter((f) => !f.own).length : 0;
  const full = others >= max;

  return (
    <div data-testid="folders-section" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, overflow: "auto" }}>
      <p className="p" style={{ padding: "0 14px 8px", fontSize: 12 }}>{t("folders.help")}</p>

      <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "0 10px 10px", flexWrap: "wrap" }}>
        <Button size="sm" variant="primary" disabled={busy !== null || full} onClick={() => void pick()}>
          {t("folders.add")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => shell.openScreen("library")}>
          {t("folders.viewLibrary")}
        </Button>
        {full ? <span className="hint">{t("folders.full", { n: max })}</span> : null}
      </div>

      {typing ? (
        <form
          aria-label={t("folders.paste.label")}
          style={{ display: "flex", gap: 8, padding: "0 10px 10px" }}
          onSubmit={(event) => {
            event.preventDefault();
            if (typed.trim()) void add(typed.trim());
          }}
        >
          <Input aria-label={t("folders.paste.label")} placeholder={t("folders.paste.placeholder")} value={typed} onChange={(event) => setTyped(event.target.value)} />
          <Button size="sm" variant="primary" type="submit" disabled={busy !== null || !typed.trim()}>
            {t("folders.paste.confirm")}
          </Button>
        </form>
      ) : null}

      {problem ? <span className="err" role="alert" style={{ padding: "0 14px 8px" }}>{problem}</span> : null}

      {folders === null && !loadError ? <Loading inline /> : null}
      {loadError && folders === null ? <ErrorState message={t("folders.error.load")} onRetry={() => void load()} /> : null}
      {folders && folders.length === 0 ? <EmptyState icon="folder" title={t("folders.empty.title")} description={t("folders.empty.body")} /> : null}
      {folders && folders.length > 0 ? (
        <ul aria-label={t("folders.list")} style={{ listStyle: "none", margin: 0, padding: "0 10px", display: "flex", flexDirection: "column", gap: 6 }}>
          {folders.map((folder) => (
            <li key={folder.id} style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Icon name="folder" size={14} />
                <span title={folder.path} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, fontSize: 13 }}>
                  {folder.own ? t("folders.own") : folder.label || lastSegment(folder.path)}
                </span>
                <Pill tone={STATUS_TONE[folder.status]}>{t(`folders.status.${folder.status}`)}</Pill>
              </div>
              <div className="mono" style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 10, color: "var(--t3)" }}>
                <span title={folder.path}>{folder.path}</span>
                <span>{folder.file_count === 1 ? t("folders.file") : t("folders.files", { n: folder.file_count })}</span>
              </div>
              {folder.status_detail ? <span className="hint" role="note">{folder.status_detail}</span> : null}
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy !== null}
                  aria-label={t("folders.rescanNamed", { name: folder.own ? t("folders.own") : folder.path })}
                  onClick={() => void rescan(folder)}
                >
                  {busy === folder.id ? t("folders.rescanning") : t("folders.rescan")}
                </Button>
                {folder.own ? null : (
                  <Button size="sm" variant="ghost" disabled={busy !== null} aria-label={t("folders.removeNamed", { name: folder.path })} onClick={() => void remove(folder)}>
                    {t("folders.remove")}
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
