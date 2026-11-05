import { useCallback, useEffect, useState } from "react";
import { useI18n } from "@/i18n";
import { StatusItem } from "@/shell/StatusItem";
import { useShellEvent } from "@/shell/useShell";
import { usageApi } from "./api";
import type { UsageData } from "./types";

const REFRESH_MS = 30_000;

/** Slot `statusBar`: "Weekly cap 38%". It shows nothing until the backend answers, and never a made-up number. */
export function UsageStatusItem() {
  const { t } = useI18n();
  const [usage, setUsage] = useState<UsageData | null>(null);

  const load = useCallback(() => {
    usageApi.weekly().then(setUsage, () => setUsage(null));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);
  useShellEvent("usage:changed", load);
  useShellEvent("settings:closed", load);

  if (!usage) return null;
  return <StatusItem label={t("models.statusbar")} value={usage.pct === null ? t("models.statusbar.unset") : `${Math.round(usage.pct)}%`} />;
}
