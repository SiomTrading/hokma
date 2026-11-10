import { useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Button, Icon, Input, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { KIND_COLOR } from "@/screens/projects/format";
import type { ProjectKind } from "@/screens/projects/types";
import type { ProjectOption, ProjectRef } from "./types";

/** More than this many projects: the first VISIBLE_WHEN_MANY show and the rest go behind "See all". */
export const MAX_ICONS = 12;
export const VISIBLE_WHEN_MANY = 11;

function colorOf(kind: string | undefined): string {
  return KIND_COLOR[kind as ProjectKind] ?? KIND_COLOR.other;
}

interface Choice {
  id: string | null;
  name: string;
  kind: string | null | undefined;
}

/**
 * What a new conversation is about (SPEC-001 addendum 0.0.7, 4.1): General (no project, the working folder) or one of
 * the projects. Choosing only changes the draft: nothing is sent until the first message. Without a choice it stays General.
 */
export function ProjectScope({
  projects,
  value,
  onChange,
  onCreate,
}: {
  projects: readonly ProjectOption[];
  value: ProjectRef | null;
  onChange: (project: ProjectRef | null) => void;
  onCreate: () => void;
}) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  const [filter, setFilter] = useState("");
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  const [focused, setFocused] = useState<number | null>(null);

  const general: Choice = { id: null, name: t("chat.scope.general"), kind: null };
  const many = projects.length > MAX_ICONS;
  const shown = useMemo(() => {
    const first = many ? projects.slice(0, VISIBLE_WHEN_MANY) : projects;
    const picked = value ? projects.find((p) => p.id === value.id) : undefined;
    // A project chosen from "See all" stays visible even when it is not among the first ones.
    return picked && !first.some((p) => p.id === picked.id) ? [...first, picked] : first;
  }, [projects, many, value]);
  const choices: Choice[] = [general, ...shown.map((p) => ({ id: p.id, name: p.name, kind: p.kind }))];
  const selected = choices.findIndex((c) => c.id === (value?.id ?? null));
  const current = selected === -1 ? 0 : selected;

  const choose = (choice: Choice) => onChange(choice.id === null ? null : { id: choice.id, name: choice.name });

  // The arrows move the focus; Space or Enter (a click on the button) choose.
  const move = (index: number) => {
    const next = (index + choices.length) % choices.length;
    setFocused(next);
    buttons.current[next]?.focus();
  };
  const stop = focused !== null && focused < choices.length ? focused : current;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      move(stop + 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      move(stop - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      move(0);
    } else if (event.key === "End") {
      event.preventDefault();
      move(choices.length - 1);
    }
  };

  const matching = projects.filter((p) => p.name.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <div className="chat-scope" data-testid="project-scope">
      <p className="lbl chat-scope-label" id="chat-scope-label">
        {t("chat.scope.label")}
      </p>
      <div role="radiogroup" aria-labelledby="chat-scope-label" className="chat-scope-row" onKeyDown={onKeyDown}>
        {choices.map((choice, index) => {
          const on = index === current;
          return (
            <button
              key={choice.id ?? "general"}
              ref={(node) => {
                buttons.current[index] = node;
              }}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={index === stop ? 0 : -1}
              onFocus={() => setFocused(index)}
              className={on ? "chat-scope-opt on" : "chat-scope-opt"}
              title={choice.id === null ? t("chat.scope.generalHint") : choice.name}
              data-project={choice.id ?? "general"}
              onClick={() => choose(choice)}
            >
              <span className="chat-scope-icon" style={choice.kind ? { color: colorOf(choice.kind) } : undefined}>
                <Icon name={choice.id === null ? "home" : "folder"} size={22} />
              </span>
              <span className="chat-scope-name">{choice.name}</span>
            </button>
          );
        })}
        {many ? (
          <button type="button" className="chat-scope-opt" onClick={() => setAll(true)}>
            <span className="chat-scope-icon">
              <Icon name="plus" size={22} />
            </span>
            <span className="chat-scope-name">{t("chat.scope.more")}</span>
          </button>
        ) : null}
      </div>
      {projects.length === 0 ? (
        <button type="button" className="chat-link" onClick={onCreate}>
          {t("chat.scope.create")}
        </button>
      ) : null}

      {all ? (
        <Modal label={t("chat.scope.label")} onClose={() => setAll(false)} style={{ width: "min(420px, 100%)", flexDirection: "column", padding: 22, gap: 12 }}>
          <h2 className="h1">{t("chat.scope.label")}</h2>
          <Input
            autoFocus
            type="search"
            aria-label={t("chat.scope.search")}
            placeholder={t("chat.scope.search")}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <ul className="chat-scope-all">
            {matching.length === 0 ? <li className="p">{t("chat.scope.none")}</li> : null}
            {matching.map((project) => (
              <li key={project.id}>
                <button
                  type="button"
                  className="conv"
                  aria-label={t("chat.scope.pick", { name: project.name })}
                  onClick={() => {
                    onChange({ id: project.id, name: project.name });
                    setAll(false);
                    setFilter("");
                  }}
                >
                  <span style={{ color: colorOf(project.kind), display: "inline-flex" }}>
                    <Icon name="folder" size={16} />
                  </span>
                  <span className="chat-conv-title">{project.name}</span>
                </button>
              </li>
            ))}
          </ul>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setAll(false)}>
              {t("chat.list.cancel")}
            </Button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
