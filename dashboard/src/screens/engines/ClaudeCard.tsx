import { useCallback, useRef } from "react";
import { Button, Dot, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { ClaudeStep } from "../first-run/ClaudeStep";
import type { ClaudeDto } from "../first-run/types";
import { enginesApi, errorKey, type Connection } from "./api";

interface ClaudeCardProps {
  /** The saved Claude connection, or undefined once it was disconnected (or never made). */
  row: Connection | undefined;
  /** What `claude auth status` says right now; null while it is being read. */
  claude: ClaudeDto | null;
  /** Runs an action, shows its error and reloads the list. */
  act: (fn: () => Promise<unknown>) => Promise<void>;
  /** The Claude CLI now says this (after a sign-in or a check): refresh the card and, when connected, the saved connection. */
  onState: (state: ClaudeDto["state"]) => void;
}

type View = "loading" | "connected" | "disconnected" | "expired" | "notInstalled";

function viewOf(claude: ClaudeDto | null, row: Connection | undefined): View {
  if (claude === null) return "loading";
  if (claude.state === "no_cli") return "notInstalled";
  if (claude.state === "no_session") return "expired";
  return row ? "connected" : "disconnected";
}

/**
 * Claude in Settings > Engines. It is always listed, whatever the saved connections are, so a student whose session
 * expired or who disconnected it can sign in again from here with buttons (bug 1123): the status comes from the Claude
 * CLI itself, the buttons from what can be done in that status.
 */
export function ClaudeCard({ row, claude, act, onState }: ClaudeCardProps) {
  const { t } = useI18n();
  const view = viewOf(claude, row);
  const latest = useRef(onState);
  latest.current = onState;
  const stateChanged = useCallback((state: ClaudeDto["state"]) => latest.current(state), []);
  const tone = view === "connected" ? "ok" : "warn";
  const label = row?.label ?? "Claude";

  return (
    <div className="pane" data-testid="claude-card" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <strong>{label}</strong>
        {view === "loading" ? null : (
          <Pill tone={tone}>
            <Dot />
            {view === "connected" && claude?.plan ? t("firstrun.claude.connectedPlan", { plan: claude.plan }) : t(`eng.claude.state.${view}`)}
          </Pill>
        )}
        {row?.is_default ? <Pill tone="ok">{t("eng.active")}</Pill> : null}
      </div>
      {view === "loading" ? <Loading inline label={t("firstrun.claude.checking")} /> : null}
      {view === "disconnected" ? <p className="p">{t("eng.claude.disconnectedBody")}</p> : null}
      {view === "connected" && row && row.status === "error" && row.status_detail ? (
        <p className="err" role="alert" style={{ fontSize: 12, color: "var(--bad)" }}>
          {t(errorKey(row.status_detail), { provider: label, cli: label })}
        </p>
      ) : null}
      {view === "expired" || view === "notInstalled" ? <ClaudeStep compact known={claude?.state} onState={stateChanged} /> : null}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {view === "disconnected" ? (
          <Button size="sm" variant="primary" onClick={() => void act(() => enginesApi.claudeConnection())}>
            {t("eng.claude.connect")}
          </Button>
        ) : null}
        {row && view !== "notInstalled" ? (
          <>
            {!row.is_default ? (
              <Button size="sm" onClick={() => void act(() => enginesApi.makeDefault(row.id))}>
                {t("eng.makeActive")}
              </Button>
            ) : null}
            <Button size="sm" onClick={() => void act(() => enginesApi.test(row.id))}>
              {t("eng.test")}
            </Button>
          </>
        ) : null}
        {row ? (
          <Button size="sm" variant="ghost" onClick={() => void act(() => enginesApi.remove(row.id))}>
            {t("eng.disconnect")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
