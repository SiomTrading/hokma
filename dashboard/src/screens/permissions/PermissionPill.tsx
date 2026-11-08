import { useEffect, useRef, useState } from "react";
import { Icon, Menu, MenuItem } from "@/design";
import { useI18n } from "@/i18n";
import { useShellEvent } from "@/shell/useShell";
import "./permissions.css";
import { refreshMode, trackTurn } from "./store";
import { useModeControl } from "./useModeControl";
import type { PermissionMode } from "./types";

const MODES: PermissionMode[] = ["ask", "auto"];
const MODE_DOT: Record<PermissionMode, string> = { ask: "var(--acc)", auto: "var(--org)" };

/** The shield pill of the composer: the current mode, and a menu with the two modes. */
export function PermissionPill({ running }: { running: boolean }) {
  const { t } = useI18n();
  const control = useModeControl();
  const [open, setOpen] = useState(false);
  const pill = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    void refreshMode();
  }, []);
  // The setup closes: the server may have written the default mode of a new installation.
  useShellEvent("setup:changed", () => void refreshMode());

  useEffect(() => {
    if (!running) return;
    trackTurn(true);
    return () => trackTurn(false);
  }, [running]);

  if (!control.loaded) return null;

  return (
    <>
      <Menu open={open} onClose={() => setOpen(false)} label={t("permissions.menu")} triggerRef={pill} className="perm-menu">
        <div className="chat-menu-head">
          <span className="lbl">{t("permissions.title")}</span>
        </div>
        {MODES.map((mode) => (
          <MenuItem
            key={mode}
            selected={mode === control.mode}
            dotColor={MODE_DOT[mode]}
            hint={t(`permissions.mode.${mode}.hint`)}
            onClick={() => {
              setOpen(false);
              control.choose(mode);
            }}
          >
            {t(`permissions.mode.${mode}`)}
          </MenuItem>
        ))}
      </Menu>
      <button
        ref={pill}
        type="button"
        className={`pill perm-pill ${control.mode === "auto" ? "org" : "acc"}`}
        data-testid="permission-pill"
        data-mode={control.mode}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("permissions.pill.label", { mode: t(`permissions.mode.${control.mode}`) })}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="shield" size={13} strokeWidth={2} />
        {t(`permissions.pill.${control.mode}`)}
      </button>
      {control.pending ? (
        <span className="chat-small perm-pending" role="status">
          {t("permissions.pending")}
        </span>
      ) : null}
      {control.dialog}
    </>
  );
}
