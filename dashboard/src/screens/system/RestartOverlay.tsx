import { Button, Isotype, Pane, cx } from "@/design";
import { dismissFailure, useRestartState, watchEngine } from "@/api";
import { useI18n } from "@/i18n";

/** Covers the whole page while the engine restarts after an update, or says why it could not start. */
export function RestartOverlay() {
  const { t } = useI18n();
  const restart = useRestartState();
  if (restart.phase === "idle") return null;
  if (restart.phase === "off") {
    return (
      <div
        className={cx("hk", document.documentElement.dataset.theme === "light" && "light")}
        data-testid="shutdown-screen"
        style={{ position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center", padding: 16, background: "var(--bg)" }}
      >
        <main role="status" aria-live="polite" style={{ maxWidth: 460, display: "flex", flexDirection: "column", gap: 14, alignItems: "center", textAlign: "center" }}>
          <Isotype size={56} />
          <h1 className="disp">{t("shutdown.offTitle")}</h1>
          <p className="p">{t("shutdown.offBody")}</p>
        </main>
      </div>
    );
  }
  const failed = restart.phase === "failed";
  return (
    <div className={cx("hk", document.documentElement.dataset.theme === "light" && "light")} style={{ position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center", padding: 16, background: "var(--scrim)", backdropFilter: "blur(6px)" }}>
      <Pane role={failed ? "alertdialog" : "status"} aria-live="polite" aria-label={failed ? t("restart.failedTitle") : t("restart.title")} style={{ maxWidth: 460, padding: "22px 24px", display: "flex", flexDirection: "column", gap: 12, alignItems: "center", textAlign: "center" }}>
        {failed ? null : <span className="spin" aria-hidden="true" />}
        <h2 className="h2">{failed ? t("restart.failedTitle") : t("restart.title")}</h2>
        {failed ? (
          <>
            <p className="p">{restart.logPath ? t("restart.failedBody", { path: restart.logPath }) : t("restart.failedBodyNoPath")}</p>
            <Button
              size="sm"
              onClick={() => {
                dismissFailure();
                watchEngine({ mode: "restart" });
              }}
            >
              {t("restart.tryAgain")}
            </Button>
          </>
        ) : (
          <p className="p">{t("restart.body")}</p>
        )}
      </Pane>
    </div>
  );
}
