import { useApiQuery } from "@/api";
import { Button, Dot, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { formatShortcut, SHORTCUTS } from "@/shell/shortcuts";
import { FIRST_DAY_STEPS } from "./types";
import type { FirstDayProgress } from "./types";

const CARD = { padding: 18, display: "flex", flexDirection: "column", gap: 8 } as const;
const PRE = { margin: 0, fontSize: 12, lineHeight: 1.7, color: "var(--t1)", whiteSpace: "pre-wrap" } as const;
const COMMANDS = ["start", "doctor", "update", "owner"] as const;
const SLASH = ["modelo", "agente", "proyecto"] as const;

/** "Your first day with Hokma": the three steps with their real progress. */
export function FirstDayCard({ onOpenGuide }: { onOpenGuide?: () => void }) {
  const { t } = useI18n();
  const progress = useApiQuery<FirstDayProgress>("/api/learn/progress");
  const data = progress.data;

  return (
    <Pane style={CARD} as="section" aria-label={t("learn.firstDay.title")}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Pill tone="acc">{t("learn.firstDay.pill")}</Pill>
        {data ? <Pill tone={data.done === data.total ? "ok" : "default"}>{t("learn.firstDay.progress", { done: data.done, total: data.total })}</Pill> : null}
      </div>
      <h2 className="h2">{t("learn.firstDay.title")}</h2>
      {progress.loading && !data ? <Loading inline /> : null}
      {data ? (
        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {FIRST_DAY_STEPS.map((step, index) => {
            const done = data.steps[step];
            return (
              <li key={step} className="p" style={{ display: "flex", gap: 8, alignItems: "center", color: done ? "var(--t1)" : undefined }}>
                <Dot color={done ? "var(--ok)" : "var(--t3)"} />
                <span>
                  {index + 1}. {t(`learn.firstDay.step.${step}`)}
                </span>
                <span className="sr-only" style={{ position: "absolute", left: -9999 }}>
                  {t(done ? "learn.firstDay.done" : "learn.firstDay.pending")}
                </span>
              </li>
            );
          })}
        </ol>
      ) : null}
      {onOpenGuide ? (
        <div>
          <Button size="sm" variant="ghost" onClick={onOpenGuide}>
            {t("learn.firstDay.open")}
          </Button>
        </div>
      ) : null}
    </Pane>
  );
}

/** "Quick commands": the four terminal commands. */
export function CommandsCard() {
  const { t } = useI18n();
  return (
    <Pane style={CARD} as="section" aria-label={t("learn.commands.title")}>
      <Pill style={{ alignSelf: "flex-start" }}>{t("learn.commands.pill")}</Pill>
      <h2 className="h2">{t("learn.commands.title")}</h2>
      <dl style={{ ...PRE, fontSize: 13, display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 14, rowGap: 6, alignItems: "baseline", margin: 0 }}>
        {COMMANDS.map((name) => (
          <div key={name} style={{ display: "contents" }}>
            <dt className="mono" style={{ whiteSpace: "nowrap", fontSize: 12 }}>{`hokma ${name}`}</dt>
            <dd style={{ margin: 0, color: "var(--t2)" }}>{t(`learn.commands.${name}`)}</dd>
          </div>
        ))}
      </dl>
    </Pane>
  );
}

/** "Shortcuts": keyboard shortcuts from `shell/shortcuts.ts` and the chat's slash commands. */
export function ShortcutsCard() {
  const { t } = useI18n();
  return (
    <Pane style={CARD} as="section" aria-label={t("learn.shortcuts.title")}>
      <Pill style={{ alignSelf: "flex-start" }}>{t("learn.shortcuts.pill")}</Pill>
      <h2 className="h2">{t("learn.shortcuts.title")}</h2>
      <span className="lbl">{t("learn.shortcuts.keyboard")}</span>
      <dl className="mono" style={{ ...PRE, display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 14, rowGap: 2 }}>
        {SHORTCUTS.map((shortcut) => (
          <div key={shortcut.id} style={{ display: "contents" }}>
            <dt>{formatShortcut(shortcut)}</dt>
            <dd style={{ margin: 0, color: "var(--t2)" }}>{t(shortcut.labelKey)}</dd>
          </div>
        ))}
      </dl>
      <span className="lbl">{t("learn.shortcuts.slash")}</span>
      <dl className="mono" style={{ ...PRE, display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 14, rowGap: 2 }}>
        {SLASH.map((name) => (
          <div key={name} style={{ display: "contents" }}>
            <dt>/{name}</dt>
            <dd style={{ margin: 0, color: "var(--t2)" }}>{t(`learn.slash.${name}`)}</dd>
          </div>
        ))}
      </dl>
    </Pane>
  );
}
