import { useCallback, useEffect, useState } from "react";
import { Button, ErrorState, Glyph, Loading, Modal, Option, Pill, Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { agentsApi } from "../agents/api";
import { agentTint, MODELS, shortDescription } from "../agents/format";
import { OpusConfirm } from "../agents/OpusConfirm";
import type { AgentItem } from "../agents/types";
import { deskAgentsApi, errorCode } from "./agentsApi";
import { failText } from "./configApi";
import type { DeskModel } from "./types";

interface Props {
  deskId: number;
  deskName: string;
  /** Slugs already in the desk. */
  taken: string[];
  onClose: () => void;
  /** Called with the name of the agent that was brought, so the tab can refresh and say so. */
  onBrought: (name: string) => void;
}

/** Picks an agent that exists and brings it to the desk with the model chosen for it. The agent is not copied. */
export function BringAgentModal({ deskId, deskName, taken, onClose, onBrought }: Props) {
  const { t } = useI18n();
  const [items, setItems] = useState<AgentItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [slug, setSlug] = useState<string | null>(null);
  const [model, setModel] = useState<DeskModel>("sonnet");
  const [askOpus, setAskOpus] = useState(false);
  const [needsOn, setNeedsOn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoadError(null);
    deskAgentsApi
      .catalog()
      .then((data) => setItems([...(data.orchestrator ? [data.orchestrator] : []), ...data.agents]))
      .catch((cause: unknown) => setLoadError(failText(cause, t("desks.agents.bring.loadError"))));
  }, [t]);
  useEffect(load, [load]);

  const free = (items ?? []).filter((item) => !taken.includes(item.slug));
  const picked = free.find((item) => item.slug === slug) ?? null;

  const pick = (item: AgentItem) => {
    setSlug(item.slug);
    setModel(item.model);
    setNeedsOn(false);
    setError(null);
  };

  const bring = async (turnOn: boolean) => {
    if (!picked) return;
    setBusy(true);
    setError(null);
    try {
      if (turnOn) await agentsApi.setStatus(picked.slug, "active");
      await deskAgentsApi.add(deskId, { agent_slug: picked.slug, model });
      onBrought(picked.name);
      onClose();
    } catch (cause) {
      if (errorCode(cause).endsWith("agent_off")) setNeedsOn(true);
      else setError(failText(cause, t("desks.agents.bring.error")));
      setBusy(false);
    }
  };

  return (
    <>
      <Modal label={t("desks.agents.bring.title", { name: deskName })} onClose={onClose} style={{ width: "min(560px, 94vw)" }}>
        <div className="desk-bring">
          <h2 className="h1">{t("desks.agents.bring.title", { name: deskName })}</h2>
          <p className="hint">{t("desks.agents.bring.text")}</p>
          {loadError ? <ErrorState message={loadError} onRetry={load} /> : null}
          {!loadError && items === null ? <Loading label={t("desks.agents.loading")} /> : null}
          {items !== null && free.length === 0 ? <p className="hint">{t("desks.agents.bring.empty")}</p> : null}
          <div role="radiogroup" aria-label={t("desks.agents.bring")} className="desk-bring-list">
            {free.map((item) => (
              <Option key={item.slug} selected={item.slug === slug} onSelect={() => pick(item)} className="desk-bring-row">
                <Glyph initial={item.name.slice(0, 2).toUpperCase()} color={agentTint(item.slug)} size={34} />
                <span className="desk-bring-text">
                  <b>{item.name}</b>
                  <span className="hint">{shortDescription(item.description)}</span>
                </span>
                <Pill tone={item.active ? "default" : "warn"}>{item.active ? t("desks.agents.bring.unassigned") : t("desks.agents.bring.off")}</Pill>
              </Option>
            ))}
          </div>
          {picked ? (
            <div className="desk-bring-model">
              <span className="lbl">{t("desks.agents.bring.model")}</span>
              <Segmented
                label={t("desks.agents.bring.model")}
                value={model}
                onChange={(next) => (next === "opus" ? setAskOpus(true) : setModel(next))}
                options={MODELS.map((m) => ({ value: m, label: t(`desks.model.${m}`) }))}
              />
            </div>
          ) : null}
          {needsOn && picked ? (
            <p className="hint" role="status">
              {t("desks.agents.bring.offAsk", { name: picked.name })}
            </p>
          ) : null}
          {error ? (
            <p className="err" role="alert">
              {error}
            </p>
          ) : null}
          <div className="desk-bring-actions">
            <Button variant="ghost" onClick={onClose}>
              {t("desks.agents.bring.cancel")}
            </Button>
            <Button variant="primary" disabled={busy || !picked} onClick={() => void bring(needsOn)}>
              {needsOn ? t("desks.agents.bring.turnOn") : t("desks.agents.bring.submit")}
            </Button>
          </div>
        </div>
      </Modal>
      {askOpus ? (
        <OpusConfirm
          onCancel={() => setAskOpus(false)}
          onConfirm={() => {
            setAskOpus(false);
            setModel("opus");
          }}
        />
      ) : null}
    </>
  );
}
