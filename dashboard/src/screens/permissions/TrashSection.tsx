import { useCallback, useEffect, useState } from "react";
import { Button, EmptyState, ErrorState, Loading, Modal, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import "./permissions.css";
import { permissionErrorText, permissionsApi } from "./api";
import { formatSize, formatWhen } from "./format";
import { RestorePoints } from "./RestorePoints";
import type { TrashItem, TrashList } from "./types";

type Confirming = { kind: "purge"; item: TrashItem } | { kind: "empty" } | null;

function ConfirmModal({ confirming, busy, onCancel, onConfirm }: { confirming: Exclude<Confirming, null>; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const { t } = useI18n();
  const purge = confirming.kind === "purge";
  const title = purge ? t("trash.purge.title") : t("trash.empty.title");
  return (
    <Modal label={title} onClose={onCancel} style={{ width: "min(440px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{title}</h2>
      <p className="p">{purge ? t("trash.purge.body", { name: confirming.item.name }) : t("trash.empty.body")}</p>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onCancel}>
          {t("trash.cancel")}
        </Button>
        <Button variant="danger" disabled={busy} onClick={onConfirm}>
          {purge ? t("trash.purge.confirm") : t("trash.empty.confirm")}
        </Button>
      </div>
    </Modal>
  );
}

function TrashRow({ item, busy, locale, onRestore, onPurge }: { item: TrashItem; busy: boolean; locale: string; onRestore: () => void; onPurge: () => void }) {
  const { t } = useI18n();
  return (
    <li>
      <Pane className="perm-row" data-testid="trash-item">
        <div className="perm-row-head">
          <span className="h2" style={{ overflowWrap: "anywhere" }}>
            {item.name}
          </span>
          <div className="perm-actions">
            {!item.present ? <Pill tone="warn">{t("trash.missing")}</Pill> : null}
            <Button size="sm" disabled={busy || !item.present} onClick={onRestore}>
              {t("trash.restore")}
            </Button>
            <Button size="sm" variant="danger" disabled={busy} onClick={onPurge}>
              {t("trash.purge")}
            </Button>
          </div>
        </div>
        <span className="mono perm-path" title={t("trash.originalPath")}>
          {item.original_path}
        </span>
        <div className="perm-meta">
          {item.project_name ? <span>{t("trash.project", { name: item.project_name })}</span> : null}
          {item.conversation_title ? <span>{t("trash.conversation", { title: item.conversation_title })}</span> : null}
          <span>{formatSize(item.size_bytes)}</span>
          <span>{formatWhen(item.moved_at, locale)}</span>
        </div>
        {item.reason ? <span className="p" style={{ fontSize: 12 }}>{t("trash.reason", { reason: item.reason })}</span> : null}
      </Pane>
    </li>
  );
}

/** Settings > Trash: what the agents moved instead of deleting, and the restore points taken before each turn. */
export function TrashSection() {
  const { t, locale } = useI18n();
  const shell = useShell();
  const [data, setData] = useState<TrashList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<Confirming>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await permissionsApi.trash());
    } catch (cause) {
      setError(permissionErrorText(cause));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (work: () => Promise<string>) => {
    setBusy(true);
    try {
      shell.toast({ tone: "ok", text: await work() });
    } catch (cause) {
      shell.toast({ tone: "bad", text: permissionErrorText(cause) });
    } finally {
      setBusy(false);
      setConfirming(null);
      await load();
    }
  };

  const restore = (item: TrashItem) =>
    act(async () => {
      const done = await permissionsApi.restore(item.id);
      return done.renamed ? t("trash.restoredRenamed", { path: done.restored_to }) : t("trash.restored", { path: done.restored_to });
    });

  const confirm = () => {
    if (!confirming) return;
    if (confirming.kind === "purge") {
      const item = confirming.item;
      void act(async () => {
        await permissionsApi.purge(item.id);
        return t("trash.purged", { name: item.name });
      });
    } else {
      void act(async () => {
        const done = await permissionsApi.empty();
        return t("trash.emptied", { count: done.count });
      });
    }
  };

  let body;
  if (error) body = <ErrorState message={error} onRetry={() => void load()} />;
  else if (data === null) body = <Loading />;
  else if (data.items.length === 0) body = <EmptyState icon="trash" title={t("trash.empty.none")} description={t("trash.empty.noneBody")} />;
  else {
    body = (
      <ul className="perm-list" aria-label={t("trash.list")}>
        {data.items.map((item) => (
          <TrashRow key={item.id} item={item} busy={busy} locale={locale} onRestore={() => void restore(item)} onPurge={() => setConfirming({ kind: "purge", item })} />
        ))}
      </ul>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <section aria-label={t("trash.title")} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p className="p" style={{ fontSize: 12 }}>
          {t("trash.intro")}
        </p>
        {data ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
            <span className="p" data-testid="trash-total">
              {data.count === 1 ? t("trash.total.one", { size: formatSize(data.total_bytes) }) : t("trash.total", { count: data.count, size: formatSize(data.total_bytes) })}
            </span>
            <Button size="sm" variant="danger" disabled={busy || data.count === 0} onClick={() => setConfirming({ kind: "empty" })}>
              {t("trash.empty.button")}
            </Button>
          </div>
        ) : null}
        {body}
      </section>
      <div className="sep" />
      <RestorePoints />
      {confirming ? <ConfirmModal confirming={confirming} busy={busy} onCancel={() => setConfirming(null)} onConfirm={confirm} /> : null}
    </div>
  );
}
