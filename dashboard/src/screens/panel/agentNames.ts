import { useEffect, useState } from "react";
import { api } from "@/api";
import { useI18n } from "@/i18n";
import type { AgentsData } from "../agents/types";

const TTL_MS = 30_000;
let cache: { at: number; names: Record<string, string> } | null = null;
let inflight: Promise<Record<string, string>> | null = null;

function load(): Promise<Record<string, string>> {
  if (cache && Date.now() - cache.at < TTL_MS) return Promise.resolve(cache.names);
  inflight ??= api
    .get<AgentsData>("/api/agents")
    .then((data) => {
      const names: Record<string, string> = {};
      for (const agent of [...(data.orchestrator ? [data.orchestrator] : []), ...data.agents]) names[agent.slug] = agent.name;
      cache = { at: Date.now(), names };
      return names;
    })
    .catch(() => cache?.names ?? {})
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Tests: forget the names read before. */
export function resetAgentNames(): void {
  cache = null;
  inflight = null;
}

/** The display name of an agent from its slug: the name Hokma shows in Agents; the slug itself when it is not in the list. */
export function useAgentNamer(): (slug: string | null | undefined) => string {
  const { t } = useI18n();
  const [names, setNames] = useState<Record<string, string>>(cache?.names ?? {});
  useEffect(() => {
    let alive = true;
    void load().then((value) => alive && setNames(value));
    return () => {
      alive = false;
    };
  }, []);
  return (slug) => (slug ? (names[slug] ?? slug) : t("panel.agents.defaultName"));
}
