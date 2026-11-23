import { useCallback, useEffect, useState } from "react";
import { Button, ErrorState, Glyph, Input, Loading, Modal, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { agentsApi } from "../agents/api";
import { agentTint, shortDescription } from "../agents/format";
import { OpusConfirm } from "../agents/OpusConfirm";
import type { AgentItem } from "../agents/types";
import { useAgentName } from "./agentName";
import { deskAgentsApi } from "./agentsApi";
import { failText } from "./configApi";

interface Props {
  deskId: number;
  deskName: string;
  /** Slugs already in the desk. */
  taken: string[];
  onClose: () => void;
  /** Called with the name of the agent that was brought, so the tab can refresh and say so. */
  onBrought: (name: string) => void;
}

/** Lower case and without accents, so a search without the accent finds the word with it. */
const fold = (text: string) => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

/** Role of an agent as the catalog knows it, in words. */
function roleKey(item: AgentItem): "orchestrator" | "custom" | "specialist" {
  if (item.is_orchestrator) return "orchestrator";
  return item.is_custom ? "custom" : "specialist";
}

/**
 * Finds an agent that exists, by name, role or description, and brings it to the desk with the model it already has.
 * The agent is not copied. The model of the desk is changed afterwards in the detail of the agent.
 */
export function BringAgentModal({ deskId, deskName, taken, onClose, onBrought }: Props) {
  const { t } = useI18n();
  const nameOf = useAgentName();
  const [items, setItems] = useState<AgentItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  /** Agent waiting for the Opus confirmation. */
  const [askOpus, setAskOpus] = useState<AgentItem | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadError(null);
    deskAgentsApi
      .catalog()
      .then((data) => setItems([...(data.orchestrator ? [data.orchestrator] : []), ...data.agents]))
      .catch((cause: unknown) => setLoadError(failText(cause, t("desks.agents.bring.loadError"))));
  }, [t]);
  useEffect(load, [load]);

  const needle = fold(query.trim());
  const found = (items ?? []).filter((item) => {
    if (!needle) return true;
    const haystack = [nameOf(item.slug, item.name), item.name, t(`desks.agents.bring.role.${roleKey(item)}`), item.role ?? "", item.description];
    return fold(haystack.join(" ")).includes(needle);
  });
  /** Agents already in the desk stay in the list, marked and last, so nobody wonders where they went. */
  const rows = [...found.filter((item) => !taken.includes(item.slug)), ...found.filter((item) => taken.includes(item.slug))];

  const bring = async (item: AgentItem) => {
    setBusy(item.slug);
    setError(null);
    try {
      if (!item.active) await agentsApi.setStatus(item.slug, "active");
      await deskAgentsApi.add(deskId, { agent_slug: item.slug, model: item.model });
      onBrought(item.name);
      onClose();
    } catch (cause) {
      setError(failText(cause, t("desks.agents.bring.error")));
      setBusy(null);
    }
  };

  /** Opus costs more: it is confirmed before an agent that runs on it joins the desk. */
  const add = (item: AgentItem) => (item.model === "opus" ? setAskOpus(item) : void bring(item));

  return (
    <>
      <Modal label={t("desks.agents.bring.title", { name: deskName })} onClose={onClose} style={{ width: "min(560px, 94vw)" }}>
        <div className="desk-bring">
          <h2 className="h1">{t("desks.agents.bring.title", { name: deskName })}</h2>
          <p className="hint">{t("desks.agents.bring.text")}</p>
          <Input
            type="search"
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("desks.agents.bring.search")}
            aria-label={t("desks.agents.bring.search")}
          />
          {loadError ? <ErrorState message={loadError} onRetry={load} /> : null}
          {!loadError && items === null ? <Loading label={t("desks.agents.loading")} /> : null}
          {items !== null && rows.length === 0 ? (
            <p className="hint desk-bring-none" role="status">
              {t("desks.agents.bring.none")}
            </p>
          ) : null}
          {rows.length > 0 ? (
            <ul className="desk-bring-list" aria-label={t("desks.agents.bring")}>
              {rows.map((item) => {
                const inDesk = taken.includes(item.slug);
                const name = nameOf(item.slug, item.name);
                return (
                  <li key={item.slug} className="desk-bring-row">
                    <Glyph initial={name.slice(0, 2).toUpperCase()} color={agentTint(item.slug)} size={30} />
                    <span className="desk-bring-text">
                      <b>{name}</b>
                      <span className="hint">{shortDescription(item.description)}</span>
                    </span>
                    <Pill tone={!inDesk && !item.active ? "warn" : "default"}>
                      {!inDesk && !item.active ? t("desks.agents.bring.off") : t(`desks.agents.bring.role.${roleKey(item)}`)}
                    </Pill>
                    {inDesk ? (
                      <Pill tone="ok">{t("desks.agents.bring.inDesk")}</Pill>
                    ) : (
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={busy !== null}
                        aria-label={t("desks.agents.bring.addNamed", { name })}
                        onClick={() => add(item)}
                      >
                        {item.active ? t("desks.agents.bring.add") : t("desks.agents.bring.turnOn")}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
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
          </div>
        </div>
      </Modal>
      {askOpus ? (
        <OpusConfirm
          onCancel={() => setAskOpus(null)}
          onConfirm={() => {
            const item = askOpus;
            setAskOpus(null);
            void bring(item);
          }}
        />
      ) : null}
    </>
  );
}
