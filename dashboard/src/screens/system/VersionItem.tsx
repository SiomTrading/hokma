import { useEffect, useState } from "react";
import { api } from "@/api";
import { useI18n } from "@/i18n";
import { StatusItem } from "@/shell/StatusItem";

/** Slot `statusBar.end`: the installed version, read from the local server. Nothing is shown until it answers. */
export function VersionItem() {
  const { t } = useI18n();
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.get<{ version: string }>("/health").then(
      (health) => alive && setVersion(health.version),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);

  return version ? <StatusItem label={t("sys.statusBar")} value={`v${version}`} /> : null;
}
