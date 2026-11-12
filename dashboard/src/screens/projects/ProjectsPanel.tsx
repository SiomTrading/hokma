import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { DragEvent } from "react";
import { Button, Dot, EmptyState, ErrorState, Icon, IconButton, Loading, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { DocumentsSection } from "./DocumentsSection";
import { CreateProjectDialog, LinkDialog, RenameProjectDialog } from "./Dialogs";
import { PreviewDialog } from "./Preview";
import * as client from "./api";
import { entryMeta, iconFor, KIND_COLOR } from "./format";
import { getSnapshot, messageOf, openDialog, openPreview, refresh, refreshTree, subscribe } from "./store";
import type { ProjectInfo, TreeEntry } from "./types";

/** Keeps the start and the end of a long path, so the folder name stays visible. */
export function middleEllipsis(path: string, max = 34): string {
  if (path.length <= max) return path;
  const tail = Math.ceil((max - 1) * 0.65);
  const head = max - 1 - tail;
  return `${path.slice(0, head)}\u2026${path.slice(path.length - tail)}`;
}

/** Header button of the right column: opens the create dialog (rendered by the panel body). */
export function CreateProjectButton() {
  const { t } = useI18n();
  return <IconButton icon="plus" size={15} label={t("projects.create")} onClick={() => openDialog("create")} />;
}

function metaText(t: (k: string, p?: Record<string, string | number>) => string, p: ProjectInfo): string {
  const key = p.git.ok ? (p.files === 1 ? "projects.fileGit" : "projects.filesGit") : p.files === 1 ? "projects.fileNoGit" : "projects.filesNoGit";
  return t(key, { n: p.files });
}

const plainButton = { background: "none", border: 0, width: "100%", cursor: "pointer", textAlign: "left" } as const;

function TreeRows({ entries, onOpen }: { entries: TreeEntry[]; onOpen: (entry: TreeEntry) => void }) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const hidden = (path: string) => {
    for (const folder of collapsed) if (path.startsWith(`${folder}/`)) return true;
    return false;
  };
  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (!next.delete(path)) next.add(path);
      return next;
    });

  return (
    <div role="tree" aria-label={t("projects.contents")}>
      {entries
        .filter((e) => !hidden(e.path))
        .map((entry) => {
          const isDir = entry.type === "dir";
          const off = collapsed.has(entry.path);
          return (
            <button
              key={entry.path}
              type="button"
              role="treeitem"
              aria-expanded={isDir ? !off : undefined}
              aria-label={isDir ? t(off ? "projects.expand" : "projects.collapse", { name: entry.name }) : t("projects.open", { name: entry.name })}
              className={isDir ? "tr" : "tr f"}
              style={{ ...plainButton, paddingLeft: 8 + entry.depth * 14 + (isDir ? 0 : 18) }}
              onClick={() => (isDir ? toggle(entry.path) : onOpen(entry))}
            >
              <Icon name={iconFor(entry)} size={14} />
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{entry.name}</span>
              <span style={{ marginLeft: "auto", font: "400 10px JetBrains Mono, monospace", color: "var(--t3)" }}>
                {entryMeta(entry)}
              </span>
            </button>
          );
        })}
    </div>
  );
}

export function ProjectsPanel() {
  const { t } = useI18n();
  const shell = useShell();
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const [dragging, setDragging] = useState(false);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [tab, setTab] = useState<"files" | "documents">("files");
  const picker = useRef<HTMLInputElement>(null);
  const announced = useRef<number | null>(null);

  const { list } = snap;
  const active = list?.projects.find((p) => p.id === list.active_id) ?? null;

  useEffect(() => {
    void refresh();
  }, []);

  // Tell the other panels (chat, terminal) which project is active, once loaded and on every change.
  useEffect(() => {
    if (!list) return;
    const id = active ? active.id : 0; // 0: General, no project
    if (announced.current === id) return;
    announced.current = id;
    shell.emit("project:active", active ? { id: String(active.id), name: active.name } : { id: null, name: null });
  }, [list, active, shell]);

  const chooseGeneral = useCallback(async () => {
    if (!list || list.active_id === null) return;
    try {
      await client.deactivateProject();
      await refresh();
      shell.toast({ tone: "info", text: t("projects.toast.general") });
    } catch (cause) {
      shell.toast({ tone: "bad", text: messageOf(cause) });
    }
  }, [list, shell, t]);

  const choose = useCallback(
    async (project: ProjectInfo) => {
      if (project.active) return;
      try {
        await client.activateProject(project.id);
        await refresh();
        shell.toast({ tone: "info", text: t("projects.toast.active", { name: project.name }) });
      } catch (cause) {
        shell.toast({ tone: "bad", text: messageOf(cause) });
      }
    },
    [shell, t],
  );

  const upload = useCallback(
    async (files: File[]) => {
      if (!active || files.length === 0) return;
      for (const file of files) {
        if (file.size > client.MAX_UPLOAD_BYTES) {
          shell.toast({ tone: "warn", text: t("projects.toast.tooBig", { name: file.name }) });
          continue;
        }
        setBusyName(file.name);
        try {
          const done = await client.uploadProjectFile(active.id, file);
          shell.toast({
            tone: done.git.ok ? "ok" : "warn",
            text: done.git.ok ? t(done.created ? "projects.toast.uploaded" : "projects.toast.exists", { path: done.path }) : t("projects.gitOffToast"),
          });
        } catch (cause) {
          shell.toast({ tone: "bad", text: messageOf(cause) });
        }
      }
      setBusyName(null);
      await refresh();
    },
    [active, shell, t],
  );

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    void upload(Array.from(event.dataTransfer?.files ?? []));
  };
  const onDragOver = (event: DragEvent) => {
    if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return;
    event.preventDefault();
    setDragging(true);
  };

  if (snap.loading && !list) return <Loading />;
  if (snap.listError && !list) return <ErrorState title={t("projects.error.load")} message={snap.listError} onRetry={() => void refresh()} />;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div style={{ padding: "0 14px 10px", display: "flex", flexDirection: "column", gap: 4 }}>
        <span className="lbl">{t("projects.workdir")}</span>
        <span className="mono" title={list?.workdir} style={{ fontSize: 11, color: "var(--t2)", whiteSpace: "nowrap" }}>
          {middleEllipsis(list?.workdir ?? "")}
        </span>
      </div>

      {list && list.projects.length === 0 ? (
        <EmptyState icon="folder" title={t("projects.empty.title")} description={t("projects.empty.body")} action={<Button size="sm" variant="primary" onClick={() => openDialog("create")}>{t("projects.create")}</Button>} />
      ) : (
        <div role="list" aria-label={t("projects.list")} style={{ display: "flex", flexDirection: "column", gap: 2, padding: "0 6px" }}>
          <div role="listitem">
            <button type="button" data-testid="project-general" aria-current={list && list.active_id === null ? true : undefined} className={list && list.active_id === null ? "proj on" : "proj"} onClick={() => void chooseGeneral()}>
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Dot color="var(--t3)" />
                <span className="h2" style={{ fontSize: 13 }}>{t("projects.general")}</span>
              </span>
              <span className="p" style={{ fontSize: 11 }}>{t("projects.generalHint")}</span>
            </button>
          </div>
          {list?.projects.map((p) => (
            <div key={p.id} role="listitem">
              <button type="button" aria-current={p.active || undefined} className={p.active ? "proj on" : "proj"} onClick={() => void choose(p)}>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Dot color={KIND_COLOR[p.kind]} />
                  <span className="h2" style={{ fontSize: 13 }}>{p.name}</span>
                </span>
                <span className="p" style={{ fontSize: 11 }}>{metaText(t, p)}</span>
              </button>
            </div>
          ))}
        </div>
      )}

      {active ? (
        <>
          <div className="sep" style={{ margin: "8px 14px" }} />
          <div style={{ padding: "0 14px 6px", display: "flex", flexDirection: "column", gap: 2 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="lbl">{t("projects.contents")}</span>
              <Button size="sm" variant="ghost" style={{ marginLeft: "auto", minHeight: 24, padding: "0 8px" }} onClick={() => openDialog("rename")}>
                {t("projects.rename")}
              </Button>
            </div>
            <span className="mono" title={active.path} style={{ fontSize: 10, color: "var(--t3)", whiteSpace: "nowrap" }}>{middleEllipsis(active.path)}</span>
            {!active.git.ok && active.exists ? <span className="hint" role="note">{t("projects.gitOff")}</span> : null}
          </div>

          <div style={{ padding: "0 14px 8px" }}>
            <Segmented
              label={t("docs.tab.label")}
              value={tab}
              onChange={setTab}
              options={[
                { value: "files", label: t("docs.tab.files") },
                { value: "documents", label: t("docs.tab.documents") },
              ]}
            />
          </div>

          {tab === "documents" ? <DocumentsSection key={active.id} projectId={active.id} /> : null}

          {tab === "files" ? (
          <>
          <div
            style={{ flex: 1, overflow: "auto", padding: "0 6px", outline: dragging ? "1px dashed var(--acc)" : "none", borderRadius: 10 }}
            onDragOver={onDragOver}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            data-testid="project-tree"
          >
            {!active.exists ? <p className="p" style={{ padding: "0 8px" }}>{t("projects.missing")}</p> : null}
            {snap.treeLoading && !snap.tree ? <Loading inline /> : null}
            {snap.treeError ? <ErrorState message={snap.treeError} onRetry={() => void refreshTree()} /> : null}
            {snap.tree && snap.tree.entries.length === 0 ? <p className="p" style={{ padding: "0 8px" }}>{t("projects.noFiles")}</p> : null}
            {snap.tree ? (
              <TreeRows entries={snap.tree.entries} onOpen={(e) => openPreview({ projectId: active.id, path: e.path, size: e.size ?? 0 })} />
            ) : null}
            {snap.tree?.truncated ? <p className="hint" style={{ padding: "0 8px" }}>{t("projects.truncated")}</p> : null}
          </div>

          <div
            role="group"
            aria-label={t("projects.drop.area")}
            onDragOver={onDragOver}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            style={{
              margin: 10, padding: 12, border: `1px dashed ${dragging ? "var(--acc)" : "var(--border)"}`, borderRadius: 12,
              display: "flex", flexDirection: "column", gap: 4, textAlign: "center",
            }}
          >
            <span className="p" style={{ fontSize: 12 }}>
              {busyName ? t("projects.drop.busy", { name: busyName }) : dragging ? t("projects.drop.active") : t("projects.drop.title")}
            </span>
            <span className="p" style={{ fontSize: 11 }}>{t("projects.drop.sub")}</span>
            <div style={{ display: "flex", justifyContent: "center", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
              <Button size="sm" onClick={() => picker.current?.click()} disabled={busyName !== null}>
                {t("projects.pick")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => openDialog("link")}>
                {t("projects.addLink")}
              </Button>
            </div>
            <input
              ref={picker}
              type="file"
              multiple
              hidden
              data-testid="project-file-input"
              onChange={(event) => {
                void upload(Array.from(event.target.files ?? []));
                event.target.value = "";
              }}
            />
          </div>
          </>
          ) : null}
        </>
      ) : null}

      {snap.dialog === "create" ? (
        <CreateProjectDialog
          onClose={() => openDialog(null)}
          onCreated={async (project) => {
            openDialog(null);
            try {
              await client.activateProject(project.id);
            } catch (cause) {
              shell.toast({ tone: "bad", text: messageOf(cause) });
            }
            await refresh();
            shell.toast({ tone: "ok", text: t("projects.toast.created", { name: project.name }) });
          }}
        />
      ) : null}
      {snap.dialog === "rename" && active ? (
        <RenameProjectDialog
          project={active}
          onClose={() => openDialog(null)}
          onRenamed={async () => {
            openDialog(null);
            announced.current = null;
            await refresh();
            shell.toast({ tone: "ok", text: t("projects.toast.renamed") });
          }}
        />
      ) : null}
      {snap.dialog === "link" && active ? (
        <LinkDialog
          project={active}
          onClose={() => openDialog(null)}
          onAdded={async (result) => {
            openDialog(null);
            await refresh();
            shell.toast({ tone: "ok", text: result.file ? t("projects.toast.linkDownloaded", { path: result.file }) : t("projects.toast.linkSaved") });
          }}
        />
      ) : null}
      {snap.preview ? <PreviewDialog {...snap.preview} onClose={() => openPreview(null)} /> : null}
    </div>
  );
}
