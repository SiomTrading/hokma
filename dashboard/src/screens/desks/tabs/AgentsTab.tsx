import { useCallback, useEffect, useRef, useState } from "react";
import { Button, cx, EmptyState, ErrorState, Glyph, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { agentTint } from "../../agents/format";
import { CreateAgentModal } from "../../agents/CreateAgentModal";
import { AgentDetail } from "../AgentDetail";
import { useAgentName } from "../agentName";
import { deskAgentsApi, errorCode } from "../agentsApi";
import type { AgentState, DeskAgent, DeskUsage } from "../agentsApi";
import { BringAgentModal } from "../BringAgentModal";
import { BudgetMeter } from "../BudgetMeter";
import { failText } from "../configApi";
import { formatTokens } from "../format";
import { refreshDesks } from "../store";
import type { DeskModel } from "../types";
import type { TabProps } from "./types";

const GROUPS: readonly AgentState[] = ["working", "idle", "off"];
const GROUP_TONE = { working: "ok", idle: "warn", off: "default" } as const;
/** While the tab is open the state of the team (who is working) is read again at this pace. */
const REFRESH_MS = 15_000;

/**
 * Team of one desk, grouped by state with the detail of the selected agent on the right, and the weekly token budget
 * of the desk above. The agents are global; what is stored per desk is the link and the model.
 */
export function AgentsTab({ desk }: TabProps) {
  const { t, locale } = useI18n();
  const nameOf = useAgentName();
  const shell = useShell();
  const [agents, setAgents] = useState<DeskAgent[] | null>(null);
  const [usage, setUsage] = useState<DeskUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [bringing, setBringing] = useState(false);
  const [creating, setCreating] = useState(false);
  const ticket = useRef(0);

  const load = useCallback(() => {
    const mine = (ticket.current += 1);
    Promise.all([deskAgentsApi.list(desk.id), deskAgentsApi.usage(desk.id)])
      .then(([team, week]) => {
        if (mine !== ticket.current) return;
        setError(null);
        setAgents(team);
        setUsage(week);
      })
      .catch((cause: unknown) => {
        if (mine === ticket.current) setError(failText(cause, t("desks.agents.error.load")));
      });
  }, [desk.id, t]);

  useEffect(() => {
    setAgents(null);
    setUsage(null);
    setSelected(null);
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      ticket.current += 1;
    };
  }, [load]);

  /** Reads the team again and the list of desks too, whose cards and bar carry the counts and the budget state. */
  const reload = useCallback(() => {
    load();
    void refreshDesks();
  }, [load]);

  const team = agents ?? [];
  const current = team.find((item) => item.slug === selected) ?? team[0] ?? null;
  const working = team.filter((item) => item.state === "working").length;
  const tokens = team.reduce((sum, item) => sum + item.tokens_week, 0);
  const maxTokens = team.reduce((max, item) => Math.max(max, item.tokens_week), 0);

  const act = async (run: () => Promise<unknown>, failure: string, done?: () => void) => {
    setBusy(true);
    setActionError(null);
    try {
      await run();
      done?.();
      reload();
    } catch (cause) {
      setActionError(errorCode(cause).endsWith("chief_required") ? t("desks.agents.error.chief") : failText(cause, failure));
    } finally {
      setBusy(false);
    }
  };

  const changeModel = (agent: DeskAgent, model: DeskModel) =>
    void act(() => deskAgentsApi.update(desk.id, agent.slug, { model }), t("desks.agents.error.save"));
  const changeActive = (agent: DeskAgent, active: boolean) =>
    void act(() => deskAgentsApi.update(desk.id, agent.slug, { active }), t("desks.agents.error.save"));
  const remove = (agent: DeskAgent) =>
    void act(() => deskAgentsApi.remove(desk.id, agent.slug), t("desks.agents.error.remove"), () => {
      setSelected(null);
      shell.toast({ tone: "ok", text: t("desks.agents.removed", { name: nameOf(agent.slug, agent.name) }) });
    });

  /** A new agent is created globally (existing flow) and then linked to this desk with the model it was created with. */
  const afterCreate = async (created: { slug: string; name: string; model: DeskModel }) => {
    try {
      await deskAgentsApi.add(desk.id, { agent_slug: created.slug, model: created.model, role: "custom" });
      shell.toast({ tone: "ok", text: t("desks.agents.bring.done", { name: nameOf(created.slug, created.name) }) });
      setSelected(created.slug);
    } catch {
      shell.toast({ tone: "warn", text: t("desks.agents.create.bringError") });
    }
    reload();
  };

  if (error && agents === null) return <ErrorState message={error} onRetry={load} />;
  if (agents === null || usage === null) return <Loading label={t("desks.agents.loading")} />;

  return (
    <div className="desk-agents">
      <div className="pane desk-agents-stats">
        <div>
          <div className="lbl">{t("desks.agents.working")}</div>
          <div className="mono desk-stat-big">
            {working} <span className="desk-stat-of">{t("desks.agents.workingOf", { total: team.length })}</span>
          </div>
        </div>
        <div>
          <div className="lbl">{t("desks.agents.tokens")}</div>
          <div className="mono desk-stat-big">{formatTokens(tokens, locale)}</div>
        </div>
        <BudgetMeter usage={usage} />
      </div>
      <div className="pane desk-agents-list">
        <div className="ph">
          <span className="title">{t("desks.agents.team")}</span>
          <span className="x desk-agents-tools">
            <Button variant="primary" onClick={() => setBringing(true)}>
              {t("desks.agents.bring")}
            </Button>
            <Button onClick={() => setCreating(true)}>
              {t("desks.agents.create")}
            </Button>
          </span>
        </div>
        <div className="scroll desk-agents-rows" role="region" aria-label={t("desks.agents.list")}>
          {team.length === 0 ? <EmptyState icon="agents" title={t("desks.agents.empty")} /> : null}
          {GROUPS.map((group) => {
            const rows = team.filter((item) => item.state === group);
            if (rows.length === 0) return null;
            return (
              <div key={group} role="group" aria-label={t(`desks.agents.group.${group}`)}>
                <div className="desk-agents-group">
                  <Pill tone={GROUP_TONE[group]}>{t(`desks.agents.group.${group}`)}</Pill>
                  <span className="mono hint">{rows.length}</span>
                </div>
                {rows.map((item) => (
                  <button
                    key={item.slug}
                    type="button"
                    className={cx("desk-agent-row", item.slug === current?.slug && "on")}
                    aria-label={t("desks.agents.select", { name: nameOf(item.slug, item.name) })}
                    aria-current={item.slug === current?.slug ? "true" : undefined}
                    onClick={() => {
                      setSelected(item.slug);
                      setActionError(null);
                    }}
                  >
                    <Glyph initial={nameOf(item.slug, item.name).slice(0, 2).toUpperCase()} color={agentTint(item.slug)} size={34} />
                    <span className="desk-agent-row-text">
                      <b>{nameOf(item.slug, item.name)}</b>
                      <span className="hint">{item.last_action ?? t("desks.agents.noAction")}</span>
                    </span>
                    <Pill>{t(`desks.model.${item.model}`)}</Pill>
                    <span className="mono hint">{formatTokens(item.tokens_week, locale)}</span>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </div>
      <div className="pane desk-agents-detail">
        {current ? (
          <AgentDetail
            key={current.slug}
            agent={current}
            maxTokens={maxTokens}
            busy={busy}
            error={actionError}
            onModel={(model) => changeModel(current, model)}
            onActive={(active) => changeActive(current, active)}
            onRemove={() => remove(current)}
          />
        ) : (
          <p className="hint desk-agents-pick">{t("desks.agents.pick")}</p>
        )}
      </div>
      {bringing ? (
        <BringAgentModal
          deskId={desk.id}
          deskName={desk.name}
          taken={team.map((item) => item.slug)}
          onClose={() => setBringing(false)}
          onBrought={(name) => {
            shell.toast({ tone: "ok", text: t("desks.agents.bring.done", { name }) });
            reload();
          }}
        />
      ) : null}
      {creating ? (
        <CreateAgentModal onClose={() => setCreating(false)} onCreated={(created) => void afterCreate(created)} />
      ) : null}
    </div>
  );
}
