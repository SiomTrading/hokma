import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api";
import { Button, Dot, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { setupApi } from "../first-run/api";
import type { ClaudeDto } from "../first-run/types";
import { enginesApi, errorKey, type Connection } from "./api";
import { ClaudeCard } from "./ClaudeCard";
import { ConnectCards } from "./ConnectCards";
import { RegionCard } from "./RegionCard";
import { useEngines } from "./useEngines";

function tone(c: Connection): "ok" | "warn" | "bad" {
  return c.status === "ok" || c.status === "connected" ? "ok" : c.status === "error" ? "bad" : "warn";
}

/** The saved Claude connection of the subscription (an imported or key-based Claude Code is listed like any other). */
function isClaudeRow(c: Connection): boolean {
  return c.engine === "claude_code" && (!c.auth_kind || c.auth_kind === "subscription");
}

/** Settings > Engines: connect, change the active engine, test and disconnect. Claude is always the first card. */
export function EnginesSettings() {
  const { t } = useI18n();
  const { data, failed, reload } = useEngines();
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [claude, setClaude] = useState<ClaudeDto | null>(null);

  // The saved connection can say "connected" while the Claude session expired, and it can be gone after a disconnect
  // (bugs 1112, 1123): the status shown is the CLI's own.
  const readClaude = useCallback(async () => {
    try {
      setClaude(await setupApi.claude());
    } catch {
      setClaude(null);
    }
  }, []);
  useEffect(() => {
    void readClaude();
  }, [data, readClaude]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await reload();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  if (!data) {
    return failed ? (
      <p className="err" role="alert">
        {t("eng.loadFailed")}
      </p>
    ) : (
      <Loading inline label={t("eng.loading")} />
    );
  }
  const list = data.connections.filter((c) => c.status !== "removed");
  const claudeRow = list.find(isClaudeRow);
  const others = list.filter((c) => c !== claudeRow);

  // The Claude CLI changed state (the student signed in, or the session ended): show it in the pill. After a sign-in
  // make sure Claude is a saved connection and that its saved status is not an old error.
  const claudeChanged = async (state: ClaudeDto["state"]) => {
    if (state === "connected") {
      await act(async () => {
        if (claudeRow) {
          if (claudeRow.status !== "connected") await enginesApi.test(claudeRow.id);
        } else {
          await enginesApi.claudeConnection();
        }
      });
    }
    await readClaude();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <p className="p">{t("eng.settings.lead")}</p>
      {list.length === 0 ? <p className="p">{t("eng.none_banner")}</p> : null}
      <ClaudeCard row={claudeRow} claude={claude} act={act} onState={(state) => void claudeChanged(state)} />
      {others.map((c) => (
        <div key={c.id} className="pane" style={{ padding: 14, display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <strong>{c.label}</strong>
            <Pill tone={tone(c)}>
              <Dot />
              {c.status}
            </Pill>
            {c.is_default ? <Pill tone="ok">{t("eng.active")}</Pill> : null}
          </div>
          {c.status === "error" && c.status_detail === "region" ? (
            <RegionCard provider={c.label} onRetry={() => void act(() => enginesApi.test(c.id))} onOther={() => setAdding(true)} />
          ) : c.status === "error" && c.status_detail ? (
            <p className="err" role="alert" style={{ fontSize: 12, color: "var(--bad)" }}>
              {t(errorKey(c.status_detail), { provider: c.label, cli: c.label })}
            </p>
          ) : null}
          <div style={{ display: "flex", gap: 8 }}>
            {!c.is_default ? (
              <Button size="sm" onClick={() => void act(() => enginesApi.makeDefault(c.id))}>
                {t("eng.makeActive")}
              </Button>
            ) : null}
            <Button size="sm" onClick={() => void act(() => enginesApi.test(c.id))}>
              {t("eng.test")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void act(() => enginesApi.remove(c.id))}>
              {t("eng.disconnect")}
            </Button>
          </div>
        </div>
      ))}
      {error ? (
        <p className="err" role="alert" style={{ color: "var(--bad)", fontSize: 12 }}>
          {error}
        </p>
      ) : null}
      {adding ? (
        <ConnectCards listing={data} onChanged={() => void reload()} />
      ) : (
        <Button style={{ alignSelf: "flex-start" }} variant="primary" onClick={() => setAdding(true)}>
          {t("eng.connectAnother")}
        </Button>
      )}
    </div>
  );
}
