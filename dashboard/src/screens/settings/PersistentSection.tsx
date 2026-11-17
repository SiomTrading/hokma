import { useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { ErrorState, Loading, Pane, Pill, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";

/** `wake` is filled by the wake-up scheduler when present; without it the screen derives the state. */
export interface PowerState {
  keep_awake: boolean;
  wake_enabled: boolean;
  /** The real state of the block on system sleep (not the saved preference). */
  active: boolean;
  /** A turn or routine is running now (older engines do not send it). */
  working?: boolean;
  supported: boolean;
  wake?: { status: "ok" | "needs_permission" | "unsupported"; at?: string | null; message?: string } | null;
}

export type KeepAwakeStatus = "off" | "idle" | "protecting" | "failed";

/** The pill comes from the real block: on and holding, on but nothing running, on and running but not holding. */
export function keepAwakeStatus(state: PowerState): KeepAwakeStatus {
  if (!state.keep_awake || !state.supported) return "off";
  if (state.active) return "protecting";
  return state.working ? "failed" : "idle";
}

type Preference = "keep_awake" | "wake_enabled";

export function wakeStatus(state: PowerState): "off" | "ok" | "needs_permission" | "unsupported" {
  if (!state.supported || state.wake?.status === "unsupported") return "unsupported";
  if (!state.wake_enabled) return "off";
  return state.wake?.status ?? "ok";
}

const KEEP_TONE = { off: undefined, idle: undefined, protecting: "ok", failed: "warn" } as const;

/** "ok" with no wake-up time means there is nothing to wake for. */
export function wakeMessageKey(state: PowerState, wake: ReturnType<typeof wakeStatus>): string {
  if (wake === "ok" && !state.wake?.at) return "persist.wake.ok.none.message";
  return `persist.wake.${wake}.message`;
}

/** "07:58" in the user's language, from the ISO time the engine sends; "" when it cannot be read. */
export function wakeTime(at: string | null | undefined, locale: string): string {
  if (!at) return "";
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

const TONE = { off: undefined, ok: "ok", needs_permission: "warn", unsupported: "warn" } as const;

/** Settings > Keep working: keep the computer awake while Hokma works, wake it for the next routine. */
export function PersistentSection() {
  const { t, locale } = useI18n();
  const power = useApiQuery<PowerState>("/api/power");
  const [saved, setSaved] = useState<PowerState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  if (power.loading && !power.data) return <Loading />;
  if (power.error || !power.data) return <ErrorState message={power.error?.message} onRetry={power.reload} />;
  const state = saved ?? power.data;
  const wake = wakeStatus(state);
  const keep = keepAwakeStatus(state);
  const wakeAt = wake === "ok" ? wakeTime(state.wake?.at, locale) : "";

  const change = async (key: Preference, value: boolean) => {
    setSaving(true);
    setError(null);
    try {
      setSaved(await api.put<PowerState>("/api/power", { [key]: value }));
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Pane style={{ padding: "4px 0" }}>
        <Row style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span className="h2">{t("persist.keepAwake")}</span>
            <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Pill tone={KEEP_TONE[keep]} data-testid="keep-status">{t(`persist.keep.${keep}`)}</Pill>
              <span className="p" style={{ fontSize: 12 }} data-testid="keep-message">{t(!state.supported ? "persist.unsupported" : `persist.keep.${keep}.message`)}</span>
            </span>
          </span>
          <Toggle checked={state.keep_awake} label={t("persist.keepAwake")} disabled={saving || !state.supported} onChange={(v) => void change("keep_awake", v)} />
        </Row>
        <Row style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span className="h2">{t("persist.wake")}</span>
            <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Pill tone={TONE[wake]} data-testid="wake-status">{t(`persist.wake.${wake}`)}</Pill>
              <span className="p" style={{ fontSize: 12 }} data-testid="wake-message">{state.wake?.message || t(wakeMessageKey(state, wake))}</span>
              {wakeAt ? <span className="p" style={{ fontSize: 12 }} data-testid="wake-at">{t("persist.wake.at", { time: wakeAt })}</span> : null}
            </span>
          </span>
          <Toggle checked={state.wake_enabled} label={t("persist.wake")} disabled={saving || wake === "unsupported"} onChange={(v) => void change("wake_enabled", v)} />
        </Row>
      </Pane>
      {error !== null ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
      <p className="p" style={{ fontSize: 12 }}>{t("persist.powerOff")}</p>
    </div>
  );
}
