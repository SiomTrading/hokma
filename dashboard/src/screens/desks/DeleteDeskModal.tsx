import { useState } from "react";
import { ApiError } from "@/api";
import { Button, Field, Input, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { desksApi } from "./api";
import { refreshDesks } from "./store";
import type { DeskSummary } from "./types";

interface Props {
  desk: Pick<DeskSummary, "id" | "name">;
  onClose: () => void;
}

/**
 * Confirmation to delete a desk: the exact name must be typed. When it is deleted, the tabs open on that desk close,
 * the app goes back to the Fund and the desk disappears from the menu (the list is reloaded).
 */
export function DeleteDeskModal({ desk, onClose }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const matches = typed === desk.name;

  const confirm = async () => {
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    try {
      await desksApi.remove(desk.id);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : t("desks.delete.error"));
      setBusy(false);
      return;
    }
    shell.tabs.filter((tab) => tab.screen === "desks" && tab.params.desk === String(desk.id)).forEach((tab) => shell.closeTab(tab.key));
    shell.openScreen("desks");
    shell.toast({ tone: "ok", text: t("desks.delete.done", { name: desk.name }) });
    onClose();
    void refreshDesks();
  };

  return (
    <Modal label={t("desks.delete.title")} role="alertdialog" onClose={onClose} style={{ width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <h2 className="h1">{t("desks.delete.title")}</h2>
      <p className="p">{t("desks.delete.body", { name: desk.name })}</p>
      <Field label={t("desks.delete.type", { name: desk.name })} error={error ?? undefined}>
        {(props) => (
          <Input {...props} value={typed} autoComplete="off" disabled={busy} onChange={(event) => setTyped(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void confirm()} />
        )}
      </Field>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          {t("desks.delete.cancel")}
        </Button>
        <Button variant="danger" disabled={!matches || busy} onClick={() => void confirm()}>
          {t("desks.delete.confirm")}
        </Button>
      </div>
    </Modal>
  );
}
