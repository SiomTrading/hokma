import { useState } from "react";
import { Button, Glyph, Meter, Pill, Segmented, Toggle } from "@/design";
import { getCatalog, useI18n } from "@/i18n";
import { agentTint, MODELS } from "../agents/format";
import { OpusConfirm } from "../agents/OpusConfirm";
import { useAgentName } from "./agentName";
import type { DeskAgent } from "./agentsApi";
import { formatTokens } from "./format";
import type { DeskModel } from "./types";

interface Props {
  agent: DeskAgent;
  /** Largest weekly use among the agents of the desk, to draw the bar. */
  maxTokens: number;
  busy: boolean;
  error: string | null;
  onModel: (model: DeskModel) => void;
  onActive: (active: boolean) => void;
  onRemove: () => void;
}

/** Detail of one agent of the desk: what it can and cannot do, its model on this desk, its use and its activity. */
export function AgentDetail({ agent, maxTokens, busy, error, onModel, onActive, onRemove }: Props) {
  const { t, locale } = useI18n();
  const nameOf = useAgentName();
  const agentLabel = nameOf(agent.slug, agent.name);
  const [askOpus, setAskOpus] = useState(false);
  const [askRemove, setAskRemove] = useState(false);
  const catalog = getCatalog(locale);
  /** A message key resolves to its text; anything else is text the student wrote and shows as it is. */
  const text = (entry: string) => catalog[entry] ?? entry;
  const roleKey = `desks.role.${agent.role}`;
  const roleName = catalog[roleKey] ? t(roleKey) : agent.role;
  const fallbackRole = (kind: "can" | "cannot") => (catalog[`desks.agents.${kind}.${agent.role}`] ? agent.role : "custom");
  const can = agent.can.length > 0 ? agent.can.map(text) : [t(`desks.agents.can.${fallbackRole("can")}`)];
  const cannot = agent.cannot.length > 0 ? agent.cannot.map(text) : [t(`desks.agents.cannot.${fallbackRole("cannot")}`)];
  const isChief = agent.role === "chief";
  const tone = agent.state === "working" ? "ok" : agent.state === "idle" ? "warn" : "default";
  const share = maxTokens > 0 ? (agent.tokens_week / maxTokens) * 100 : 0;

  return (
    <div className="desk-agent-detail">
      <div className="desk-agent-head">
        <Glyph initial={agentLabel.slice(0, 2).toUpperCase()} color={agentTint(agent.slug)} />
        <div className="desk-agent-title">
          <b>{agentLabel}</b>
          <span className="hint">{roleName}</span>
        </div>
        <Pill tone={tone}>{t(`desks.agents.group.${agent.state}`)}</Pill>
      </div>
      <div className="desk-agent-body">
        <div>
          <div className="lbl">{t("desks.agents.model")}</div>
          <Segmented
            label={t("desks.agents.model")}
            value={agent.model}
            onChange={(next) => (next === "opus" ? setAskOpus(true) : next !== agent.model ? onModel(next) : undefined)}
            options={MODELS.map((m) => ({ value: m, label: t(`desks.model.${m}`) }))}
          />
          <div className="hint">{t(`desks.agents.model.hint.${agent.model}`)}</div>
          <div className="hint">{t("desks.agents.model.scope")}</div>
        </div>
        <div className="desk-agent-cans">
          <div className="desk-can">
            <div className="lbl">{t("desks.agents.can")}</div>
            {can.map((line) => (
              <p key={line}>{line}</p>
            ))}
          </div>
          <div className="desk-cannot">
            <div className="lbl">{t("desks.agents.cannot")}</div>
            {cannot.map((line) => (
              <p key={line}>{line}</p>
            ))}
          </div>
        </div>
        <div>
          <div className="kv">
            <span className="lbl">{t("desks.agents.weekTokens")}</span>
            <span className="mono">{formatTokens(agent.tokens_week, locale)}</span>
          </div>
          <Meter value={share} label={t("desks.agents.weekTokens")} />
        </div>
        <div>
          <div className="lbl">{t("desks.agents.activity")}</div>
          {agent.activity.length === 0 ? <p className="hint">{t("desks.agents.activity.empty")}</p> : null}
          <ul className="desk-activity">
            {agent.activity.map((item, index) => (
              <li key={`${index}-${item.text}`}>
                {item.at ? <span className="mono">{item.at}</span> : null}
                <span>{item.text}</span>
              </li>
            ))}
          </ul>
        </div>
        <div className="desk-agent-switch">
          <span>{t("desks.agents.active")}</span>
          <Toggle checked={agent.active} disabled={busy} label={t("desks.agents.active")} onChange={onActive} />
        </div>
        {error ? (
          <p className="err" role="alert">
            {error}
          </p>
        ) : null}
        <div className="desk-agent-actions">
          {askRemove ? (
            <div className="desk-remove-ask" role="group" aria-label={t("desks.agents.remove")}>
              <p>{t("desks.agents.remove.ask", { name: agentLabel })}</p>
              <div className="desk-agent-actions">
                <Button size="sm" variant="ghost" onClick={() => setAskRemove(false)}>
                  {t("desks.agents.remove.cancel")}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() => {
                    setAskRemove(false);
                    onRemove();
                  }}
                >
                  {t("desks.agents.remove.confirm")}
                </Button>
              </div>
            </div>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy || isChief}
              title={isChief ? t("desks.agents.remove.chief") : undefined}
              onClick={() => setAskRemove(true)}
            >
              {t("desks.agents.remove")}
            </Button>
          )}
        </div>
      </div>
      {askOpus ? (
        <OpusConfirm
          onCancel={() => setAskOpus(false)}
          onConfirm={() => {
            setAskOpus(false);
            if (agent.model !== "opus") onModel("opus");
          }}
        />
      ) : null}
    </div>
  );
}
