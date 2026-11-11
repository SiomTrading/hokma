import { useEffect, useState } from "react";
import { ApiError, useApiQuery } from "@/api";
import { Button, Dot, EmptyState, ErrorState, Glyph, Loading, Pane, Pill, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { agentsApi } from "./api";
import { CreateAgentModal } from "./CreateAgentModal";
import { agentTint, nextModel, shortDescription } from "./format";
import { OpusConfirm } from "./OpusConfirm";
import type { AgentItem, AgentsData, ModelAlias } from "./types";

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** Settings > Agents: the orchestrator on top, the others below, each with its model pill and its on/off switch. */
export function AgentsSettings() {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<AgentsData>("/api/agents");
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opusFor, setOpusFor] = useState<AgentItem | null>(null);

  // An agent the chat creates or edits shows up here without leaving the screen.
  const { reload } = list;
  useEffect(() => {
    const timer = window.setInterval(reload, 4000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const change = async (agent: AgentItem, work: () => Promise<AgentItem>, toastKey: string) => {
    setBusy(agent.slug);
    setError(null);
    try {
      const updated = await work();
      shell.toast({ tone: "ok", text: t(toastKey, { name: updated.name, model: t(`agents.model.${updated.model}`) }) });
      list.reload();
      shell.emit("agents:changed");
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(null);
    }
  };

  /** The kit's agents read from the catalog in the user's language; the owner's own agents show their own text. */
  const describe = (agent: AgentItem): string => {
    if (!agent.is_custom) {
      const key = `agents.kit.${agent.slug}.desc`;
      const text = t(key);
      if (text !== key) return text;
    }
    return shortDescription(agent.description);
  };

  if (list.loading && !list.data) return <Loading />;
  if (list.error || !list.data) return <ErrorState message={list.error?.message} onRetry={list.reload} />;
  const data = list.data;
  const everyone = [...(data.orchestrator ? [data.orchestrator] : []), ...data.agents];

  if (everyone.length === 0) {
    return (
      <Pane>
        <EmptyState
          icon="agents"
          title={t("agents.empty.title")}
          description={t("agents.empty.body")}
          action={<Button variant="primary" onClick={() => setCreating(true)}>{t("agents.create")}</Button>}
        />
        {creating ? <CreateAgentModal onClose={() => setCreating(false)} onCreated={list.reload} /> : null}
      </Pane>
    );
  }

  const setModel = (agent: AgentItem, model: ModelAlias) =>
    change(agent, () => agentsApi.setModel(agent.slug, model), "agents.changed.model");
  const cycle = async (agent: AgentItem) => {
    const next = nextModel(agent.model);
    if (next === "opus") {
      setOpusFor(agent);
      return;
    }
    await setModel(agent, next);
  };
  const toggle = (agent: AgentItem, on: boolean) =>
    change(agent, () => agentsApi.setStatus(agent.slug, on ? "active" : "off"), on ? "agents.changed.on" : "agents.changed.off");

  return (
    <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {opusFor ? (
        <OpusConfirm
          onCancel={() => setOpusFor(null)}
          onConfirm={() => {
            const target = opusFor;
            setOpusFor(null);
            void setModel(target, "opus");
          }}
        />
      ) : null}
      <span className="p" aria-live="polite">{t("agents.count", { active: data.active, total: data.total })}</span>
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}

      {data.orchestrator ? (
        <div
          style={{ display: "flex", alignItems: "center", gap: 12, padding: 14, borderRadius: 14, background: "var(--acc-sub)", border: "1px solid var(--border)", flexWrap: "wrap" }}
        >
          <Glyph initial={data.orchestrator.name.slice(0, 1).toUpperCase()} style={{ background: "var(--acc)", color: "var(--on-acc)" }} />
          <div style={{ flex: 1, minWidth: 200, display: "flex", flexDirection: "column", gap: 3 }}>
            <span className="h2">{t("agents.orchestrator")}: {data.orchestrator.name}</span>
            <span className="p">{t("agents.orchestrator.desc")}</span>
          </div>
          <ModelPill agent={data.orchestrator} busy={busy === data.orchestrator.slug} onCycle={() => void cycle(data.orchestrator as AgentItem)} accent />
        </div>
      ) : null}

      <div style={{ display: "flex", flexDirection: "column", gap: 4, paddingLeft: 21, borderLeft: "1px dashed var(--border)", marginLeft: 21 }}>
        {data.agents.map((agent) => (
          <div key={agent.slug} className={agent.active ? "ag on" : "ag"} style={{ marginLeft: -21 }}>
            <Glyph initial={agent.name.slice(0, 1).toUpperCase()} color={agentTint(agent.slug)} />
            <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className="h2">{agent.name}</span>
                {agent.active ? <Dot live color="var(--ok)" aria-hidden="true" /> : null}
                {agent.is_custom ? <Pill tone="acc">{t("agents.custom")}</Pill> : null}
              </div>
              <span className="p" title={describe(agent)} style={{ fontSize: 12, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                {describe(agent)}
              </span>
            </div>
            <ModelPill agent={agent} busy={busy === agent.slug} onCycle={() => void cycle(agent)} />
            <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Pill tone={agent.active ? "ok" : "default"}>
                {agent.active
                  ? t("agents.state.active")
                  : agent.enabled_session !== null
                    ? t("agents.state.session", { n: agent.enabled_session })
                    : t("agents.state.off")}
              </Pill>
              <Toggle
                checked={agent.active}
                label={t("agents.toggle", { name: agent.name })}
                disabled={busy === agent.slug}
                onChange={(on) => void toggle(agent, on)}
              />
            </span>
          </div>
        ))}
      </div>

      <Button variant="primary" style={{ alignSelf: "flex-start" }} onClick={() => setCreating(true)}>
        {data.agents.some((a) => a.is_custom) ? t("agents.create.another") : t("agents.create")}
      </Button>
      {creating ? <CreateAgentModal onClose={() => setCreating(false)} onCreated={list.reload} /> : null}
    </div>
  );
}

function ModelPill({ agent, busy, onCycle, accent }: { agent: AgentItem; busy: boolean; onCycle: () => void; accent?: boolean }) {
  const { t } = useI18n();
  const label = t(`agents.model.${agent.model}`);
  return (
    <button
      type="button"
      className={accent ? "pill acc" : "pill"}
      style={{ cursor: busy ? "progress" : "pointer", height: 30 }}
      disabled={busy}
      title={t(`agents.model.hint.${agent.model}`)}
      aria-label={t("agents.model.cycle", { name: agent.name, model: label })}
      onClick={onCycle}
    >
      {label}
    </button>
  );
}
