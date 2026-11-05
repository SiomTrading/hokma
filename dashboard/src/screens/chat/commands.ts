import { MODELS } from "./types";
import type { ChatOptions } from "./types";

export type CommandName = "model" | "agent" | "project";

const ALIASES: Record<string, CommandName> = {
  modelo: "model",
  model: "model",
  agente: "agent",
  agent: "agent",
  proyecto: "project",
  project: "project",
};

const COMMAND_LINE = /^\/([A-Za-z]+)(?:\s+([\s\S]*))?$/;

/** The slash command a text is, or null when it is a normal message. Same rule as the engine. */
export function parseCommand(text: string): { name: CommandName; arg: string | null } | null {
  const match = COMMAND_LINE.exec(text.trim());
  if (!match) return null;
  const name = ALIASES[match[1]!.toLowerCase()];
  if (!name) return null;
  return { name, arg: (match[2] ?? "").trim() || null };
}

export interface Suggestion {
  key: string;
  /** Text that replaces the whole input when picked. */
  value: string;
  label: string;
  hint?: string;
  dot?: string;
  /** Picking it sends the command right away (it is complete). */
  run: boolean;
}

type Translate = (key: string, params?: Record<string, string | number>) => string;

const MODEL_DOT: Record<string, string> = { haiku: "var(--ok)", sonnet: "var(--acc)", opus: "var(--org)" };

export function suggestionsFor(text: string, t: Translate, options: ChatOptions | null): Suggestion[] | null {
  if (!text.startsWith("/")) return null;
  const head = /^\/([A-Za-z]*)$/.exec(text);
  const names: Array<{ name: CommandName; key: string }> = [
    { name: "model", key: "chat.cmd.model" },
    { name: "agent", key: "chat.cmd.agent" },
    { name: "project", key: "chat.cmd.project" },
  ];
  if (head) {
    const typed = head[1]!.toLowerCase();
    return names
      .filter((entry) => t(entry.key).slice(1).startsWith(typed) || entry.name.startsWith(typed))
      .map((entry) => ({
        key: entry.name,
        value: `${t(entry.key)} `,
        label: t(entry.key),
        hint: t(`${entry.key}.help`),
        run: false,
      }));
  }
  const withArg = /^\/([A-Za-z]+)\s+([^\n]*)$/.exec(text);
  const name = withArg ? ALIASES[withArg[1]!.toLowerCase()] : undefined;
  if (!withArg || !name) return null;
  const typed = withArg[2]!.trim().toLowerCase();
  const prefix = t(`chat.cmd.${name}`);
  const match = (candidate: string) => candidate.toLowerCase().includes(typed);
  if (name === "model") {
    return MODELS.filter((m) => m.startsWith(typed)).map((m) => ({
      key: m, value: `${prefix} ${m}`, label: t(`chat.model.${m}`), hint: t(`chat.model.${m}.hint`), dot: MODEL_DOT[m], run: true,
    }));
  }
  if (name === "agent") {
    const list: Suggestion[] = [];
    if (match("none") || match(t("chat.cmd.agent.none"))) {
      list.push({ key: "none", value: `${prefix} none`, label: t("chat.cmd.agent.none"), hint: t("chat.cmd.agent.none.help"), run: true });
    }
    for (const agent of options?.agents ?? []) {
      if (match(agent.slug) || match(agent.name)) {
        list.push({ key: agent.slug, value: `${prefix} ${agent.slug}`, label: agent.slug, hint: agent.name, run: true });
      }
    }
    return list;
  }
  return (options?.projects ?? [])
    .concat(options?.default_project && !(options.projects ?? []).some((p) => p.id === options.default_project?.id) ? [options.default_project] : [])
    .filter((project) => match(project.name))
    .map((project) => ({ key: project.id, value: `${prefix} ${project.name}`, label: project.name, run: true }));
}
