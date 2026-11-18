import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { BusyConfirmDialog } from "./BusyConfirmDialog";

const PARTS = ["sqlite_vec", "embedder", "truststore", "voice"] as const;
type Part = (typeof PARTS)[number];

interface NativeHealth {
  safe_mode?: boolean;
  off?: string[];
}

/**
 * A discreet strip at the top of the Panel and the Library when /health says an optional native part is off (it
 * crashed or timed out in its probe) or Hokma runs in safe mode. It names the part, says what Hokma does without it,
 * and "Try again" clears native-probe.json and restarts the engine. Nothing is drawn while everything works.
 */
export function NativeBand() {
  const { t } = useI18n();
  const [native, setNative] = useState<NativeHealth | null>(null);
  const [asking, setAsking] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api.get<{ native?: NativeHealth }>("/health").then(
      (health) => alive && setNative(health.native ?? null),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);

  const retry = useCallback(async (force: boolean) => {
    setAsking(false);
    setFailed(false);
    try {
      await api.post(force ? "/api/native/retry?force=true" : "/api/native/retry");
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "native.in_use") setAsking(true);
      else setFailed(true);
    }
  }, []);

  const parts = (native?.off ?? []).filter((name): name is Part => (PARTS as readonly string[]).includes(name));
  const safe = native?.safe_mode === true;
  if (!safe && parts.length === 0) return null;

  return (
    <div
      role="status"
      aria-label={t("native.band.label")}
      data-testid="native-band"
      style={{
        display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "8px 12px", borderRadius: 12,
        border: "1px solid var(--border)", background: "var(--glass-subtle)", color: "var(--t2)", font: "500 12px/1.4 Montserrat, sans-serif",
      }}
    >
      <div style={{ flex: 1, minWidth: 220, display: "flex", flexDirection: "column", gap: 2 }}>
        {safe ? <span data-testid="native-band-safe">{t("native.band.safe")}</span> : null}
        {parts.map((part) => (
          <span key={part} data-testid={`native-band-${part}`}>
            {t("native.band.piece", { piece: t(`native.part.${part}`), effect: t(`native.effect.${part}`) })}
          </span>
        ))}
        {failed ? <span role="alert" style={{ color: "var(--bad)" }}>{t("native.band.retryFailed")}</span> : null}
      </div>
      <Button size="sm" onClick={() => void retry(false)} data-testid="native-band-retry">
        {t("native.band.retry")}
      </Button>
      {asking ? <BusyConfirmDialog title={t("native.band.retry")} onContinue={() => void retry(true)} onCancel={() => setAsking(false)} /> : null}
    </div>
  );
}
