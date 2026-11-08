import { useCallback, useState } from "react";
import { Button, Icon, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { permissionErrorText } from "./api";
import { applyMode, useModeSnapshot } from "./store";
import type { ModeSnapshot } from "./store";
import type { PermissionMode } from "./types";

export interface ModeControl extends ModeSnapshot {
  busy: boolean;
  /** Changes the mode. Turning "no asking" on the first time opens the short modal first. */
  choose: (mode: PermissionMode) => void;
  /** The modal, or null. Render it once next to the control. */
  dialog: React.ReactNode;
}

/** Shared by the chat pill, Settings > General > Permissions, the first-run card and the one-time notice. */
export function useModeControl(): ModeControl {
  const { t } = useI18n();
  const shell = useShell();
  const snapshot = useModeSnapshot();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);

  const commit = useCallback(
    async (mode: PermissionMode) => {
      setBusy(true);
      try {
        await applyMode(mode);
      } catch (cause) {
        shell.toast({ tone: "bad", text: permissionErrorText(cause) });
      } finally {
        setBusy(false);
      }
    },
    [shell],
  );

  const choose = useCallback(
    (mode: PermissionMode) => {
      if (busy || mode === snapshot.mode) return;
      if (mode === "auto" && !snapshot.introSeen) {
        setAsking(true);
        return;
      }
      void commit(mode);
    },
    [busy, snapshot.mode, snapshot.introSeen, commit],
  );

  const dialog = asking ? (
    <Modal label={t("permissions.modal.title")} onClose={() => setAsking(false)} style={{ width: "min(460px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Icon name="shield" size={22} />
        <h2 className="h1">{t("permissions.modal.title")}</h2>
      </div>
      <p className="p">{t("permissions.modal.body")}</p>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={() => setAsking(false)}>
          {t("permissions.modal.cancel")}
        </Button>
        <Button
          variant="primary"
          onClick={() => {
            setAsking(false);
            void commit("auto");
          }}
        >
          {t("permissions.modal.confirm")}
        </Button>
      </div>
    </Modal>
  ) : null;

  return { ...snapshot, busy, choose, dialog };
}
