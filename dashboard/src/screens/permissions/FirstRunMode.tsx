import { useEffect, useState } from "react";
import { Icon, Pane, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import "./permissions.css";
import { permissionErrorText, permissionsApi } from "./api";
import { applyMode } from "./store";
import { isMode } from "./types";
import type { PermissionMode } from "./types";

interface Props {
  /** The setup was finished before: the mode on the server is the owner's, not the default of a new installation. */
  completed: boolean;
  /** What the setup state said, for when the mode cannot be read. */
  fallback?: { permission_mode?: PermissionMode; permission_mode_intro_seen?: boolean };
}

/**
 * The "Ready" card of the first run shows the mode with its switch. A new installation starts in "no asking"
 * (the server writes it when the setup closes, unless the owner already chose); one that updated keeps its mode.
 */
export function FirstRunMode({ completed, fallback }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const [mode, setMode] = useState<PermissionMode | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const resolve = (mode: unknown, introSeen: boolean | undefined): PermissionMode => {
      const known: PermissionMode = isMode(mode) ? mode : "ask";
      return completed || introSeen === true ? known : "auto";
    };
    const fromSetup = () => resolve(fallback?.permission_mode, fallback?.permission_mode_intro_seen);
    permissionsApi.mode().then(
      (data) => alive && setMode(isMode(data?.mode) ? resolve(data.mode, data.intro_seen) : fromSetup()),
      () => alive && setMode(fromSetup()),
    );
    return () => {
      alive = false;
    };
  }, [completed, fallback]);

  if (mode === null) return null;

  const change = async (on: boolean) => {
    const target: PermissionMode = on ? "auto" : "ask";
    setBusy(true);
    try {
      await applyMode(target);
      setMode(target);
    } catch (cause) {
      shell.toast({ tone: "bad", text: permissionErrorText(cause) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8, gridColumn: "1 / -1" }} data-testid="firstrun-mode">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="shield" size={16} />
          <span className="h2">{t("permissions.mode.auto")}</span>
        </span>
        <Toggle label={t("permissions.mode.auto")} checked={mode === "auto"} disabled={busy} onChange={(on) => void change(on)} />
      </div>
      <span className="p" style={{ fontSize: 12 }}>
        {t("permissions.firstrun.body")}
      </span>
    </Pane>
  );
}
