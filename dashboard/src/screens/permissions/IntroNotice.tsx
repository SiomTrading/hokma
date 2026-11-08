import { useEffect } from "react";
import { Bubble, Button, Icon } from "@/design";
import { useI18n } from "@/i18n";
import { useShell, useShellEvent } from "@/shell/useShell";
import "./permissions.css";
import { permissionErrorText } from "./api";
import { dismissIntro, refreshMode } from "./store";
import { useModeControl } from "./useModeControl";

/**
 * The one-time notice for an installation that came from an earlier version and stays in "ask":
 * "New: let Hokma work without asking you" with "Turn on". It stays until the owner turns the mode on or says not now.
 */
export function IntroNotice() {
  const { t } = useI18n();
  const shell = useShell();
  const control = useModeControl();

  useEffect(() => {
    void refreshMode();
  }, []);
  useShellEvent("setup:changed", () => void refreshMode());

  if (!control.loaded || control.mode !== "ask" || control.introSeen) return null;

  const later = () => {
    dismissIntro().catch((cause: unknown) => shell.toast({ tone: "bad", text: permissionErrorText(cause) }));
  };

  return (
    <>
    <Bubble from="assistant" className="perm-notice fade" role="group" aria-label={t("permissions.notice.title")} data-testid="permission-notice">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Icon name="shield" size={16} />
        <span className="h2">{t("permissions.notice.title")}</span>
      </div>
      <span className="p">{t("permissions.notice.body")}</span>
      <div className="perm-notice-actions">
        <Button variant="primary" size="sm" disabled={control.busy} onClick={() => control.choose("auto")}>
          {t("permissions.notice.turnOn")}
        </Button>
        <Button variant="ghost" size="sm" disabled={control.busy} onClick={later}>
          {t("permissions.notice.later")}
        </Button>
      </div>
    </Bubble>
    {control.dialog}
    </>
  );
}
