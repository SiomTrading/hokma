import { useCallback, useEffect, useState } from "react";
import { ErrorState, Loading, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { deskConfigApi, failText } from "../../configApi";
import type { DeskConnection } from "../../configTypes";

interface Props {
  deskId: number;
}

/** Which of the global connections this desk uses. The key itself is set once, in Connections. */
export function ConnectionsSection({ deskId }: Props) {
  const { t } = useI18n();
  const [items, setItems] = useState<DeskConnection[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadError(null);
    deskConfigApi
      .connections(deskId)
      .then(setItems)
      .catch((cause: unknown) => setLoadError(failText(cause, t("desks.cfg.error.load"))));
  }, [deskId, t]);
  useEffect(load, [load]);

  const toggle = async (item: DeskConnection, enabled: boolean) => {
    setBusyId(item.provider_id);
    setError(null);
    try {
      await deskConfigApi.setConnection(deskId, item.provider_id, enabled);
      setItems((now) => (now ?? []).map((x) => (x.provider_id === item.provider_id ? { ...x, enabled } : x)));
    } catch (cause) {
      setError(failText(cause, t("desks.cfg.error.connection")));
    } finally {
      setBusyId(null);
    }
  };

  if (loadError) return <ErrorState message={loadError} onRetry={load} />;
  if (items === null) return <Loading label={t("desks.cfg.loading")} />;

  return (
    <div className="desk-cfg-form">
      <div className="lbl">{t("desks.cfg.connections.title")}</div>
      {items.length === 0 ? <p className="hint">{t("desks.cfg.connections.empty")}</p> : null}
      <ul className="desk-conn-list">
        {items.map((item) => (
          <li key={item.provider_id} className="pane desk-conn">
            <div className="desk-conn-text">
              <b>{item.name}</b>
              {item.asset_classes.length > 0 ? (
                <span className="hint">{item.asset_classes.map((c) => t(`desks.cfg.class.${c}`)).join(", ")}</span>
              ) : null}
            </div>
            <Toggle
              checked={item.enabled}
              disabled={busyId === item.provider_id}
              label={t("desks.cfg.connections.use", { name: item.name })}
              onChange={(on) => void toggle(item, on)}
            />
          </li>
        ))}
      </ul>
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
      <p className="hint desk-cfg-note">{t("desks.cfg.connections.note")}</p>
    </div>
  );
}
