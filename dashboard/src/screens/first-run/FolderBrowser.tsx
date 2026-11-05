import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api";
import { Button, ErrorState, Icon, Input, Loading, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { setupApi } from "./api";
import type { DirListing } from "./types";

interface Props {
  /** Folder to start from; empty starts at the places list. */
  start: string;
  onPick: (path: string) => void;
  onClose: () => void;
}

/** In-app folder explorer: folders only, with "New folder". */
export function FolderBrowser({ start, onPick, onClose }: Props) {
  const { t } = useI18n();
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const open = useCallback(async (path: string | null) => {
    setLoading(true);
    setError(null);
    try {
      setListing(await setupApi.dirs(path));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void open(start || null);
  }, [open, start]);

  const create = async () => {
    if (!listing?.path) return;
    setFormError(null);
    try {
      const made = await setupApi.makeDir(listing.path, newName);
      setCreating(false);
      setNewName("");
      await open(made.path);
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  const current = listing?.path ?? null;

  return (
    <Modal label={t("firstrun.browser.title")} onClose={onClose} style={{ flexDirection: "column", width: "min(620px, 100%)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
        <h2 className="h1" style={{ fontSize: 17, flex: 1 }}>
          {t("firstrun.browser.title")}
        </h2>
        <Button size="sm" variant="ghost" disabled={!listing?.parent} onClick={() => listing?.parent && void open(listing.parent)}>
          {t("firstrun.browser.up")}
        </Button>
        <Button size="sm" disabled={!current} onClick={() => setCreating((v) => !v)}>
          {t("firstrun.browser.newFolder")}
        </Button>
      </div>
      <div style={{ padding: "10px 20px 0" }}>
        <span className="mono" style={{ fontSize: 11, color: "var(--t2)", wordBreak: "break-all" }} data-testid="browser-path">
          {current ?? t("firstrun.browser.places")}
        </span>
      </div>
      {creating ? (
        <div style={{ padding: "10px 20px 0", display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <Input
              aria-label={t("firstrun.browser.newName")}
              placeholder={t("firstrun.browser.newName")}
              value={newName}
              maxLength={100}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void create()}
            />
            <Button size="sm" variant="primary" disabled={!newName.trim()} onClick={() => void create()}>
              {t("firstrun.browser.create")}
            </Button>
          </div>
          {formError ? (
            <span className="err" role="alert" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
              {formError}
            </span>
          ) : null}
        </div>
      ) : null}
      <div style={{ flex: 1, minHeight: 220, overflow: "auto", padding: "10px 20px" }}>
        {loading ? (
          <Loading inline />
        ) : error ? (
          <ErrorState message={error} onRetry={() => void open(start || null)} />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {current === null
              ? listing?.roots.map((r) => (
                  <button key={r.path} type="button" className="mi" onClick={() => void open(r.path)}>
                    <Icon name="folder" size={15} />
                    {r.kind === "home" ? t("firstrun.browser.home") : r.name}
                  </button>
                ))
              : listing?.dirs.map((d) => (
                  <button key={d.path} type="button" className="mi" onClick={() => void open(d.path)}>
                    <Icon name="folder" size={15} />
                    {d.name}
                  </button>
                ))}
            {current !== null && listing?.dirs.length === 0 ? <p className="p">{t("firstrun.browser.empty")}</p> : null}
          </div>
        )}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "14px 20px", borderTop: "1px solid var(--border)" }}>
        <Button variant="ghost" onClick={onClose}>
          {t("firstrun.cancel")}
        </Button>
        <Button variant="primary" disabled={!current} onClick={() => current && onPick(current)}>
          {t("firstrun.browser.use")}
        </Button>
      </div>
    </Modal>
  );
}
