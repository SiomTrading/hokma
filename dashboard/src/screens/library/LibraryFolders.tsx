import { useCallback, useEffect, useRef, useState } from "react";
import type { DragEvent, FormEvent } from "react";
import { ApiError } from "@/api";
import { Button, ErrorState, Icon, Input, Menu, MenuItem, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import * as docs from "@/screens/projects/documentsApi";
import * as client from "./foldersApi";
import type { FolderContents, LibraryFolder } from "./foldersApi";

/** Drag payload of a Library document row. */
export const DOCUMENT_DRAG_TYPE = "application/x-hokma-document";

/** One step of the path from the Library root to the folder being viewed. */
export interface Crumb {
  id: number;
  name: string;
}

interface Props {
  /** Path to the folder being viewed; empty at the root. */
  trail: Crumb[];
  onNavigate: (trail: Crumb[]) => void;
  /** The inline "new folder" field is open. */
  creating: boolean;
  onCloseCreate: () => void;
  /** Bumped by the screen when documents moved, so the folder list is read again. */
  version: number;
  /** A document was moved by dropping it; the document list must be read again. */
  onMoved: () => void;
}

const dragCarriesDocument = (event: DragEvent): boolean => Array.from(event.dataTransfer?.types ?? []).includes(DOCUMENT_DRAG_TYPE);

/** Drop target for documents: lights up while a document is dragged over it. */
function useDocumentDrop(onDropDocument: (documentId: number) => void) {
  const [over, setOver] = useState(false);
  return {
    over,
    handlers: {
      onDragOver: (event: DragEvent) => {
        if (!dragCarriesDocument(event)) return;
        event.preventDefault();
        setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: (event: DragEvent) => {
        if (!dragCarriesDocument(event)) return;
        event.preventDefault();
        event.stopPropagation();
        setOver(false);
        const id = Number(event.dataTransfer?.getData(DOCUMENT_DRAG_TYPE));
        if (Number.isFinite(id) && id > 0) onDropDocument(id);
      },
    },
  };
}

function Crumbs({ trail, onNavigate, onDropDocument }: { trail: Crumb[]; onNavigate: (trail: Crumb[]) => void; onDropDocument: (documentId: number, folderId: number | null) => void }) {
  const { t } = useI18n();
  return (
    <nav aria-label={t("library.crumbs.label")} className="lib-crumbs" data-testid="library-crumbs">
      <CrumbButton label={t("library.title")} current={trail.length === 0} onOpen={() => onNavigate([])} onDropDocument={(id) => onDropDocument(id, null)} />
      {trail.map((crumb, index) => (
        <span key={crumb.id} className="lib-crumb-wrap">
          <span aria-hidden className="lib-crumb-sep">&gt;</span>
          <CrumbButton
            label={crumb.name}
            current={index === trail.length - 1}
            onOpen={() => onNavigate(trail.slice(0, index + 1))}
            onDropDocument={(id) => onDropDocument(id, crumb.id)}
          />
        </span>
      ))}
    </nav>
  );
}

function CrumbButton({ label, current, onOpen, onDropDocument }: { label: string; current: boolean; onOpen: () => void; onDropDocument: (documentId: number) => void }) {
  const drop = useDocumentDrop(onDropDocument);
  return (
    <button
      type="button"
      className={`lib-crumb${current ? " current" : ""}${drop.over ? " over" : ""}`}
      aria-current={current ? "page" : undefined}
      onClick={onOpen}
      {...drop.handlers}
    >
      {label}
    </button>
  );
}

/** Name field shown in place: creating a folder or renaming one. */
function NameField({ label, initial, error, submitLabel, onSubmit, onCancel }: { label: string; initial: string; error?: string; submitLabel: string; onSubmit: (name: string) => void; onCancel: () => void }) {
  const { t } = useI18n();
  const [name, setName] = useState(initial);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(name.trim());
  };
  return (
    <form onSubmit={submit} className="lib-name-form">
      <Input
        aria-label={label}
        value={name}
        maxLength={120}
        autoFocus
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
        }}
      />
      <Button type="submit" size="sm" variant="primary">{submitLabel}</Button>
      <Button type="button" size="sm" variant="ghost" onClick={onCancel}>{t("library.folder.cancel")}</Button>
      {error ? <span className="err" role="alert">{error}</span> : null}
    </form>
  );
}

interface RowProps {
  folder: LibraryFolder;
  renaming: boolean;
  renameError?: string;
  onOpen: () => void;
  onStartRename: () => void;
  onRename: (name: string) => void;
  onCancelRename: () => void;
  onRemove: () => void;
  onDropDocument: (documentId: number) => void;
}

function FolderRow({ folder, renaming, renameError, onOpen, onStartRename, onRename, onCancelRename, onRemove, onDropDocument }: RowProps) {
  const { t } = useI18n();
  const [menu, setMenu] = useState(false);
  const trigger = useRef<HTMLSpanElement>(null);
  const drop = useDocumentDrop(onDropDocument);
  return (
    <li className={`lib-folder${drop.over ? " over" : ""}`} data-testid="library-folder" {...drop.handlers}>
      <Icon name="folder" size={14} />
      {renaming ? (
        <NameField
          label={t("library.folder.renameField", { name: folder.name })}
          initial={folder.name}
          error={renameError}
          submitLabel={t("library.folder.save")}
          onSubmit={onRename}
          onCancel={onCancelRename}
        />
      ) : (
        <>
          <button type="button" className="lib-folder-name" title={folder.name} onClick={onOpen} onDoubleClick={onStartRename}>
            {folder.name}
          </button>
          <span className="lib-menu-anchor" ref={trigger}>
            <Button size="sm" variant="ghost" aria-haspopup="menu" aria-expanded={menu} aria-label={t("library.folder.menu", { name: folder.name })} onClick={() => setMenu((open) => !open)}>
              ...
            </Button>
            <Menu open={menu} onClose={() => setMenu(false)} label={t("library.folder.menu", { name: folder.name })} placement="below" triggerRef={trigger}>
              <MenuItem
                onClick={() => {
                  setMenu(false);
                  onStartRename();
                }}
              >
                {t("library.folder.rename")}
              </MenuItem>
              <MenuItem
                onClick={() => {
                  setMenu(false);
                  onRemove();
                }}
              >
                {t("library.folder.remove")}
              </MenuItem>
            </Menu>
          </span>
        </>
      )}
    </li>
  );
}

function ConfirmRemove({ folder, contents, onCancel, onConfirm }: { folder: LibraryFolder; contents: FolderContents; onCancel: () => void; onConfirm: () => void }) {
  const { t } = useI18n();
  const docsText = t(contents.documents === 1 ? "library.folder.count.document" : "library.folder.count.documents", { n: contents.documents });
  const foldersText = t(contents.subfolders === 1 ? "library.folder.count.subfolder" : "library.folder.count.subfolders", { n: contents.subfolders });
  return (
    <Modal label={t("library.folder.confirm.title", { name: folder.name })} onClose={onCancel} style={{ width: "min(440px, 100%)" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: 24, width: "100%" }}>
        <h2 className="h2">{t("library.folder.confirm.title", { name: folder.name })}</h2>
        <p className="p">{t("library.folder.confirm.body", { documents: docsText, subfolders: foldersText })}</p>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button size="sm" variant="ghost" onClick={onCancel}>{t("library.folder.keep")}</Button>
          <Button size="sm" variant="primary" onClick={onConfirm}>{t("library.folder.remove")}</Button>
        </div>
      </div>
    </Modal>
  );
}

/** Breadcrumb path, folders of the level being viewed, inline create and rename, and removal with confirmation. */
export function LibraryFolders({ trail, onNavigate, creating, onCloseCreate, version, onMoved }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const parentId = trail.length > 0 ? trail[trail.length - 1]!.id : null;
  const [folders, setFolders] = useState<LibraryFolder[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [createError, setCreateError] = useState<string | undefined>();
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameError, setRenameError] = useState<string | undefined>();
  const [pending, setPending] = useState<{ folder: LibraryFolder; contents: FolderContents } | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const list = await client.listFolders(parentId);
      if (!alive.current) return;
      setFolders(list);
      setLoadError(false);
    } catch {
      if (alive.current) setLoadError(true);
    }
  }, [parentId]);

  useEffect(() => {
    setFolders(null);
    setRenamingId(null);
    setCreateError(undefined);
    void load();
  }, [load, version]);

  const errorOf = (cause: unknown): string =>
    cause instanceof ApiError && cause.status === 409 ? t("library.folder.error.duplicate") : t("library.folder.error.generic");

  const create = async (name: string) => {
    if (!name) {
      setCreateError(t("library.folder.error.name"));
      return;
    }
    try {
      await client.createFolder(parentId, name);
      if (!alive.current) return;
      setCreateError(undefined);
      onCloseCreate();
      await load();
    } catch (cause) {
      if (alive.current) setCreateError(errorOf(cause));
    }
  };

  const rename = async (folder: LibraryFolder, name: string) => {
    if (!name) {
      setRenameError(t("library.folder.error.name"));
      return;
    }
    if (name === folder.name) {
      setRenamingId(null);
      return;
    }
    try {
      await client.renameFolder(folder.id, name);
      if (!alive.current) return;
      setRenamingId(null);
      setRenameError(undefined);
      // A renamed folder on the path keeps the path labels true.
      onNavigate(trail.map((crumb) => (crumb.id === folder.id ? { ...crumb, name } : crumb)));
      await load();
    } catch (cause) {
      if (alive.current) setRenameError(errorOf(cause));
    }
  };

  const remove = async (folder: LibraryFolder, confirm: boolean) => {
    try {
      const result = await client.removeFolder(folder.id, confirm);
      if (!alive.current) return;
      if (!result.removed) {
        setPending({ folder, contents: result.contents });
        return;
      }
      setPending(null);
      shell.toast({ tone: "info", text: t("library.folder.toast.removed", { name: folder.name }) });
      onMoved();
      await load();
    } catch {
      if (alive.current) {
        setPending(null);
        shell.toast({ tone: "bad", text: t("library.folder.error.generic") });
      }
    }
  };

  const moveDocument = async (documentId: number, folderId: number | null, folderName: string) => {
    try {
      await docs.moveDocument(documentId, folderId);
      if (!alive.current) return;
      shell.toast({ tone: "ok", text: t("library.move.toast", { folder: folderName }) });
      onMoved();
    } catch (cause) {
      if (!alive.current) return;
      const duplicate = cause instanceof ApiError && cause.status === 409;
      shell.toast({ tone: "bad", text: t(duplicate ? "library.move.error.duplicate" : "library.move.error.generic") });
    }
  };

  return (
    <div className="lib-folders" data-testid="library-folders">
      <Crumbs trail={trail} onNavigate={onNavigate} onDropDocument={(id, folderId) => void moveDocument(id, folderId, folderId === null ? t("library.title") : trail.find((c) => c.id === folderId)?.name ?? "")} />

      {creating ? (
        <NameField label={t("library.folder.name")} initial="" error={createError} submitLabel={t("library.folder.create")} onSubmit={(name) => void create(name)} onCancel={onCloseCreate} />
      ) : null}

      {loadError && folders === null ? <ErrorState message={t("library.folder.error.load")} onRetry={() => void load()} /> : null}
      {folders && folders.length > 0 ? (
        <ul aria-label={t("library.folders.list")} className="lib-folder-list">
          {folders.map((folder) => (
            <FolderRow
              key={folder.id}
              folder={folder}
              renaming={renamingId === folder.id}
              renameError={renamingId === folder.id ? renameError : undefined}
              onOpen={() => onNavigate([...trail, { id: folder.id, name: folder.name }])}
              onStartRename={() => {
                setRenameError(undefined);
                setRenamingId(folder.id);
              }}
              onRename={(name) => void rename(folder, name)}
              onCancelRename={() => setRenamingId(null)}
              onRemove={() => void remove(folder, false)}
              onDropDocument={(id) => void moveDocument(id, folder.id, folder.name)}
            />
          ))}
        </ul>
      ) : null}

      {pending ? (
        <ConfirmRemove folder={pending.folder} contents={pending.contents} onCancel={() => setPending(null)} onConfirm={() => void remove(pending.folder, true)} />
      ) : null}
    </div>
  );
}
