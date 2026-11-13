import { useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { ErrorState, Loading, Pane, Pill, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";

/** `wake` is filled by the wake-up scheduler when present; without it the screen derives the state. */
export interface PowerState {
  keep_awake: boolean;
  wake_enabled: boolean;
  active: boolean;
  supported: boolean;
  wake?: { status: "ok" | "needs_permission" | "unsupported" } | null;
}

type Preference = "keep_awake" | "wake_enabled";

export function wakeStatus(state: PowerState): "off" | "ok" | "needs_permission" | "unsupported" {
  if (!state.supported || state.wake?.status === "unsupported") return "unsupported";
  if (!state.wake_enabled) return "off";
  return state.wake?.status ?? "ok";
}

const TONE = { off: undefined, ok: "ok", needs_permission: "warn", unsupported: "warn" } as const;

/** Settings > Keep working: keep the computer awake while Hokma works, wake it for the next routine. */
export function PersistentSection() {
  const { t } = useI18n();
  const power = useApiQuery<PowerState>("/api/power");
  const [saved, setSaved] = useState<PowerState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  if (power.loading && !power.data) return <Loading />;
  if (power.error || !power.data) return <ErrorState message={power.error?.message} onRetry={power.reload} />;
  const state = saved ?? power.data;
  const wake = wakeStatus(state);

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
            <span className="p" style={{ fontSize: 12 }}>{t(state.supported ? "persist.keepAwake.hint" : "persist.unsupported")}</span>
          </span>
          <Toggle checked={state.keep_awake} label={t("persist.keepAwake")} disabled={saving || !state.supported} onChange={(v) => void change("keep_awake", v)} />
        </Row>
        <Row style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span className="h2">{t("persist.wake")}</span>
            <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <Pill tone={TONE[wake]} data-testid="wake-status">{t(`persist.wake.${wake}`)}</Pill>
              <span className="p" style={{ fontSize: 12 }} data-testid="wake-message">{t(`persist.wake.${wake}.message`)}</span>
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
