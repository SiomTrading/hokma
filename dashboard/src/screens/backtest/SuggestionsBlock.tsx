import { useState } from "react";
import { Button, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { ACTION_TEXT_EN } from "./actions";
import { Block, Empty } from "./Block";
import type { CheckStatus, Suggestion } from "./types";

const TONE: Record<CheckStatus, "ok" | "warn" | "bad" | "default"> = { pass: "ok", warn: "warn", fail: "bad", unknown: "default", na: "default" };

/** The text the owner pastes in the chat of the creator: English, with the stable code of the action first. */
export function buildCopyText(suggestion: Suggestion): string {
  const text = ACTION_TEXT_EN[suggestion.action] ?? suggestion.text;
  const params = Object.keys(suggestion.params ?? {}).length > 0 ? `\nParams: ${JSON.stringify(suggestion.params)}` : "";
  return `action: ${suggestion.action}\nCheck ${suggestion.check} (${suggestion.key}), ${suggestion.status}: ${text}${params}`;
}

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** H: the changes the optimizer suggests to the creator, each one with a copy button. */
export function SuggestionsBlock({ suggestions }: { suggestions: Suggestion[] }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState<string | null>(null);
  const name = (key: string) => {
    const text = t(`verdict.check.${key}`);
    return text === `verdict.check.${key}` ? key : text;
  };
  return (
    <Block title={t("verdict.block.suggestions")} name="suggestions">
      {suggestions.length === 0 ? (
        <Empty>{t("verdict.sug.empty")}</Empty>
      ) : (
        <ol className="bt-suggestions">
          {suggestions.map((suggestion, index) => {
            const forOwner = suggestion.action === "answer_question";
            const id = `${suggestion.check}-${suggestion.action}`;
            return (
              <li key={id} className="bt-suggestion" data-action={suggestion.action}>
                <span className="bt-sug-no">{index + 1}.</span>
                <Pill tone={TONE[suggestion.status]}>{t(`verdict.status.${suggestion.status}`)}</Pill>
                <div className="bt-sug-main">
                  <b>{name(suggestion.key)}</b>
                  <span className="p">{forOwner ? t("verdict.sug.forOwner") : suggestion.text}</span>
                </div>
                {forOwner ? null : (
                  <Button
                    size="sm"
                    onClick={() =>
                      void copy(buildCopyText(suggestion)).then((ok) => {
                        if (ok) setCopied(id);
                      })
                    }
                    aria-label={t("verdict.sug.copyLabel", { check: name(suggestion.key) })}
                  >
                    {copied === id ? t("verdict.sug.copied") : t("verdict.sug.copy")}
                  </Button>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </Block>
  );
}
