import { useCallback, useEffect, useRef, useState } from "react";
import type { DragEvent, ReactNode } from "react";
import { ApiError } from "@/api";
import { Button, EmptyState, ErrorState, Icon, Input, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { DocumentLinks } from "./DocumentLinks";
import * as client from "./documentsApi";
import { isImageName, MAX_DOCUMENT_BYTES } from "./documentsTypes";
import type { DocsScope, DocumentInfo, DocumentStatus } from "./documentsTypes";
import { listProjects } from "./api";

/** Drag payload of a Library document row (a folder or a path segment takes it). */
const DOCUMENT_DRAG_TYPE = "application/x-hokma-document";

interface Job {
  key: number;
  name: string;
  /** Plain-language reason when the upload failed; undefined while it is running. */
  error?: string;
}

interface PendingImage {
  key: number;
  file: File;
  description: string;
}

const STATUS_TONE: Record<DocumentStatus, "ok" | "acc" | "bad"> = { ready: "ok", indexing: "acc", failed: "bad" };

let nextKey = 1;

/** How often a Library list with documents still being indexed is read again. */
const SETTLE_POLL_MS = 3000;

/**
 * Documents of one project or of the student Library: help, add and drag, list with status, remove, image
 * descriptions. In the Library each document also says which project it was uploaded from.
 */
export function DocumentsSection({
  projectId,
  folderId = null,
  reloadKey = 0,
  actions,
}: {
  projectId: DocsScope;
  /** Library only: the folder in view; null is the root. Uploads and downloads land there. */
  folderId?: number | null;
  /** Library only: changes when documents were moved elsewhere, so the list is read again. */
  reloadKey?: number;
  /** Extra controls placed next to "Add files". */
  actions?: ReactNode;
}) {
  const { t } = useI18n();
  const inLibrary = projectId === "library";
  const [projectNames, setProjectNames] = useState<Map<number, string>>(new Map());
  const shell = useShell();
  const [docs, setDocs] = useState<DocumentInfo[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [images, setImages] = useState<PendingImage[]>([]);
  const picker = useRef<HTMLInputElement>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!inLibrary) return;
    listProjects().then(
      (list) => {
        if (alive.current) setProjectNames(new Map(list.projects.map((p) => [p.id, p.name])));
      },
      () => undefined,
    );
  }, [inLibrary]);

  const load = useCallback(async () => {
    try {
      const all = await client.listDocuments(projectId, folderId);
      if (!alive.current) return;
      // The root lists only what sits at the root; a folder lists its own level.
      const list = inLibrary && folderId == null ? all.filter((doc) => !doc.library_path) : all;
      setDocs(list);
      setLoadError(false);
    } catch {
      if (alive.current) setLoadError(true);
    }
  }, [projectId, folderId, inLibrary]);

  useEffect(() => {
    setDocs(null);
    setLoadError(false);
    setJobs([]);
    setImages([]);
    void load();
  }, [load, reloadKey]);

  // Library documents stay "Indexing" until their passages are searchable: follow them until they settle.
  const settling = inLibrary && (docs?.some((doc) => doc.status === "indexing") ?? false);
  useEffect(() => {
    if (!settling) return;
    const timer = setTimeout(() => void load(), SETTLE_POLL_MS);
    return () => clearTimeout(timer);
  }, [settling, docs, load]);

  const reasonOf = useCallback(
    (cause: unknown): string => {
      if (cause instanceof ApiError) {
        if (cause.status === 409) return t(inLibrary ? "library.error.duplicate" : "docs.error.duplicate");
        if (cause.status === 413) return t("docs.error.tooBig");
        if (cause.status === 415) return t("docs.error.unsupported");
      }
      return t("docs.error.generic");
    },
    [t, inLibrary],
  );

  /** One file at a time, as the backend indexes inside the request. */
  const send = useCallback(
    async (items: { file: File; description?: string }[]) => {
      for (const { file, description } of items) {
        const key = nextKey++;
        if (file.size > MAX_DOCUMENT_BYTES) {
          setJobs((prev) => [...prev, { key, name: file.name, error: t("docs.error.tooBig") }]);
          continue;
        }
        setJobs((prev) => [...prev, { key, name: file.name }]);
        try {
          await client.uploadDocument(projectId, file, description, folderId);
          if (!alive.current) return;
          setJobs((prev) => prev.filter((j) => j.key !== key));
          shell.toast({ tone: "ok", text: t("docs.toast.added", { name: file.name }) });
          await load();
        } catch (cause) {
          if (!alive.current) return;
          setJobs((prev) => prev.map((j) => (j.key === key ? { ...j, error: reasonOf(cause) } : j)));
        }
      }
    },
    [projectId, folderId, shell, t, load, reasonOf],
  );

  const receive = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      const pictures = files.filter((f) => isImageName(f.name));
      const others = files.filter((f) => !isImageName(f.name));
      if (pictures.length > 0) setImages((prev) => [...prev, ...pictures.map((file) => ({ key: nextKey++, file, description: "" }))]);
      if (others.length > 0) void send(others.map((file) => ({ file })));
    },
    [send],
  );

  const confirmImages = () => {
    const batch = images;
    setImages([]);
    void send(batch.map(({ file, description }) => ({ file, description })));
  };

  const remove = async (doc: DocumentInfo) => {
    try {
      await client.deleteDocument(projectId, doc.id);
      shell.toast({ tone: "info", text: t("docs.toast.removed", { name: doc.name }) });
      await load();
    } catch {
      shell.toast({ tone: "bad", text: t("docs.error.remove") });
    }
  };

  const onDragOver = (event: DragEvent) => {
    if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return;
    event.preventDefault();
    setDragging(true);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    receive(Array.from(event.dataTransfer?.files ?? []));
  };

  const running = jobs.find((j) => !j.error);

  return (
    <div data-testid="documents-section" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, overflow: "auto" }}>
      <p className="p" style={{ padding: "0 14px 8px", fontSize: 12 }}>{t(inLibrary ? "library.help" : "docs.help")}</p>

      <div
        role="group"
        aria-label={t("docs.drop.area")}
        onDragOver={onDragOver}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        style={{
          margin: "0 10px 10px", padding: 12, border: `1px dashed ${dragging ? "var(--acc)" : "var(--border)"}`, borderRadius: 12,
          display: "flex", flexDirection: "column", gap: 4, textAlign: "center",
        }}
      >
        <span className="p" style={{ fontSize: 12 }}>{dragging ? t("docs.drop.active") : t("docs.drop.title")}</span>
        <span className="p" style={{ fontSize: 11 }}>{t("docs.formats")}</span>
        <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 6 }}>
          <Button size="sm" variant="primary" onClick={() => picker.current?.click()}>
            {t("docs.add")}
          </Button>
          {actions}
        </div>
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          data-testid="documents-file-input"
          onChange={(event) => {
            receive(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
      </div>

      <DocumentLinks projectId={projectId} folderId={folderId} onAdded={load} />

      {images.length > 0 ? (
        <section aria-label={t("docs.image.title")} style={{ margin: "0 10px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          <span className="lbl">{t("docs.image.title")}</span>
          <span className="hint">{t("docs.image.hint")}</span>
          {images.map((img) => (
            <div key={img.key} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span className="mono" style={{ fontSize: 11, color: "var(--t2)", overflow: "hidden", textOverflow: "ellipsis" }}>{img.file.name}</span>
              <Input
                aria-label={t("docs.image.descriptionFor", { name: img.file.name })}
                placeholder={t("docs.image.description")}
                value={img.description}
                onChange={(event) => {
                  const value = event.target.value;
                  setImages((prev) => prev.map((p) => (p.key === img.key ? { ...p, description: value } : p)));
                }}
              />
            </div>
          ))}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button size="sm" variant="ghost" onClick={() => setImages([])}>{t("docs.image.cancel")}</Button>
            <Button size="sm" variant="primary" onClick={confirmImages}>{t("docs.image.confirm")}</Button>
          </div>
        </section>
      ) : null}

      {jobs.length > 0 ? (
        <div role="status" aria-live="polite" style={{ margin: "0 10px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
          {jobs.map((job) => (
            <div key={job.key} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
              <Icon name="file" size={14} />
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", flex: 1 }}>
                {job.error ? job.name : t("docs.working", { name: job.name })}
              </span>
              {job.error ? (
                <>
                  <span className="err" role="alert">{job.error}</span>
                  <Button size="sm" variant="ghost" onClick={() => setJobs((prev) => prev.filter((j) => j.key !== job.key))}>
                    {t("docs.dismiss")}
                  </Button>
                </>
              ) : (
                <Pill tone="acc">{t("docs.status.indexing")}</Pill>
              )}
            </div>
          ))}
        </div>
      ) : null}

      {docs === null && !loadError ? <Loading inline /> : null}
      {loadError && docs === null ? <ErrorState message={t("docs.error.load")} onRetry={() => void load()} /> : null}
      {docs && docs.length === 0 && !running ? (
        <EmptyState icon="file" title={t(inLibrary ? "library.empty.title" : "docs.empty.title")} description={t(inLibrary ? "library.empty.body" : "docs.empty.body")} />
      ) : null}
      {docs && docs.length > 0 ? (
        <ul aria-label={t("docs.list")} style={{ listStyle: "none", margin: 0, padding: "0 10px", display: "flex", flexDirection: "column", gap: 6 }}>
          {docs.map((doc) => (
            <li
              key={doc.id}
              draggable={inLibrary}
              onDragStart={
                inLibrary
                  ? (event) => {
                      event.dataTransfer.setData(DOCUMENT_DRAG_TYPE, String(doc.id));
                      event.dataTransfer.effectAllowed = "move";
                    }
                  : undefined
              }
              style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 10 }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Icon name="file" size={14} />
                <span title={doc.name} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, fontSize: 13 }}>{doc.name}</span>
                <Pill tone={STATUS_TONE[doc.status]}>{t(`docs.status.${doc.status}`)}</Pill>
                <Button size="sm" variant="ghost" aria-label={t("docs.removeNamed", { name: doc.name })} onClick={() => void remove(doc)}>
                  {t("docs.remove")}
                </Button>
              </div>
              <div className="mono" style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 10, color: "var(--t3)" }}>
                <span>{doc.format.toUpperCase()}</span>
                <span>{doc.chunk_count === 1 ? t("docs.passage") : t("docs.passages", { n: doc.chunk_count })}</span>
                <span>{doc.source_url ? t("docs.origin.link") : t("docs.origin.file")}</span>
                {inLibrary && doc.origin_project_id != null && projectNames.has(doc.origin_project_id) ? (
                  <span data-testid="document-origin">{t("library.origin.from", { project: projectNames.get(doc.origin_project_id)! })}</span>
                ) : null}
              </div>
              {doc.description ? <span className="hint">{doc.description}</span> : null}
              {doc.status_detail ? <span className="hint" role="note">{doc.status_detail}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
