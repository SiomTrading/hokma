import { useEffect, useState } from "react";
import { ErrorState, Icon, Loading, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import "./permissions.css";
import { permissionErrorText, permissionsApi } from "./api";
import { refreshMode } from "./store";
import { useModeControl } from "./useModeControl";
import { isRule } from "./types";
import type { Protection } from "./types";

/** Read only. There is no switch for any of these: the list shows what Hokma refuses in both modes. */
export function ProtectionsList() {
  const { t } = useI18n();
  const [rules, setRules] = useState<Protection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setError(null);
    permissionsApi.protections().then(
      (data) => alive && setRules(data.rules ?? []),
      (cause: unknown) => alive && setError(permissionErrorText(cause)),
    );
    return () => {
      alive = false;
    };
  }, [tick]);

  if (error) return <ErrorState message={error} onRetry={() => setTick((n) => n + 1)} />;
  if (rules === null) return <Loading inline />;
  return (
    <section aria-label={t("protections.title")} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="h2">{t("protections.title")}</span>
      <p className="p" style={{ fontSize: 12 }}>
        {t("protections.body")}
      </p>
      <ul className="perm-protections" data-testid="protections">
        {rules.map((rule) => (
          <li key={rule.id} className="perm-protection">
            <Icon name="shield" size={14} />
            <span className="p">{isRule(rule.id) ? t(`protections.${rule.id}`) : rule.title}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Settings > General > Permissions: the mode in one click, and the always-on protections in read only. */
export function PermissionsSettings() {
  const { t } = useI18n();
  const control = useModeControl();

  useEffect(() => {
    void refreshMode();
  }, []);

  return (
    <section aria-label={t("permissions.title")} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="sep" />
      <span className="h2">{t("permissions.title")}</span>
      {control.loaded ? (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span className="p">{t("permissions.mode.auto")}</span>
            <span className="p" style={{ fontSize: 12 }}>
              {t("permissions.setting.body")}
            </span>
            {control.pending ? (
              <span className="chat-small" role="status">
                {t("permissions.pending")}
              </span>
            ) : null}
          </div>
          <Toggle label={t("permissions.mode.auto")} checked={control.mode === "auto"} disabled={control.busy} onChange={(on) => control.choose(on ? "auto" : "ask")} />
        </div>
      ) : (
        <Loading inline />
      )}
      <ProtectionsList />
      {control.dialog}
    </section>
  );
}
