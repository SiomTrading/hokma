import { useCallback, useRef } from "react";
import { Button, Dot, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
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
  /** The server says the student pressed Disconnect (durable, survives leaving Settings and restarts). */
  disabled?: boolean;
  /** The connection the chat will really use (data.active.id): only that one gets the Active pill. */
  activeId?: number | null;
  /** Read the Claude state again (the "Check again" button of the unknown state). */
  onRecheck?: () => void;
}

type View = "loading" | "connected" | "disconnected" | "expired" | "notInstalled" | "unknown";

function viewOf(claude: ClaudeDto | null, disabled: boolean): View {
  // The student's Disconnect wins (the Claude session outside Hokma stays open, so the CLI still says connected).
  if (disabled) return "disconnected";
  if (claude === null) return "loading";
  if (claude.state === "no_cli") return "notInstalled";
  if (claude.state === "no_session") return "expired";
  // Not being able to read the CLI is not a sign-out (bug 1128): say so, never show the sign-in.
  if (claude.state === "unknown") return "unknown";
  return "connected";
}

/**
 * Claude in Settings > Engines. It is always listed, whatever the saved connections are, so a student whose session
 * expired or who disconnected it can sign in again from here with buttons (bug 1123): the status comes from the Claude
 * CLI itself, the buttons from what can be done in that status.
 */
export function ClaudeCard({ row, claude, act, onState, disabled = false, activeId = null, onRecheck }: ClaudeCardProps) {
  const { t } = useI18n();
  const shell = useShell();
  const view = viewOf(claude, disabled);
  const latest = useRef(onState);
  latest.current = onState;
  const stateChanged = useCallback((state: ClaudeDto["state"]) => latest.current(state), []);
  const tone = view === "connected" ? "ok" : "warn";
  const label = row?.label ?? "Claude";

  // The Test button says out loud what happened: how long Claude took, or why it failed (1162).
  const probe = async (id: number) => {
    const started = performance.now();
    try {
      const res = await enginesApi.test(id);
      if (res.ok) {
        const seconds = ((performance.now() - started) / 1000).toFixed(1);
        shell.toast({ tone: "ok", text: t("eng.claude.testOk", { seconds }) });
      } else {
        shell.toast({ tone: "bad", text: t("eng.claude.testFail", { detail: t(errorKey(res.code), { provider: label, cli: label }) }) });
      }
    } catch (cause) {
      shell.toast({ tone: "bad", text: t("eng.claude.testFail", { detail: cause instanceof Error ? cause.message : String(cause) }) });
    }
  };

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
        {row && activeId === row.id && view !== "disconnected" ? <Pill tone="ok">{t("eng.active")}</Pill> : null}
      </div>
      {view === "loading" ? <Loading inline label={t("firstrun.claude.checking")} /> : null}
      {view === "disconnected" ? <p className="p">{t("eng.claude.disconnectedBody")}</p> : null}
      {view === "unknown" ? <p className="p">{t("eng.claude.unknownBody")}</p> : null}
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
        {view === "unknown" ? (
          <Button size="sm" onClick={() => onRecheck?.()}>
            {t("eng.claude.recheck")}
          </Button>
        ) : null}
        {row && view !== "notInstalled" && view !== "disconnected" ? (
          <>
            {!row.is_default ? (
              <Button size="sm" onClick={() => void act(() => enginesApi.makeDefault(row.id))}>
                {t("eng.makeActive")}
              </Button>
            ) : null}
            <Button size="sm" onClick={() => void act(() => probe(row.id))}>
              {t("eng.test")}
            </Button>
          </>
        ) : null}
        {row && view !== "disconnected" ? (
          <Button size="sm" variant="ghost" onClick={() => void act(() => enginesApi.remove(row.id))}>
            {t("eng.disconnect")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
