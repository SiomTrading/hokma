import { Bubble, Button, Icon, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import "./permissions.css";
import { isRule } from "./types";

export interface BlockData {
  id: number | string;
  rule: string;
  tool: string;
  target: string;
}

/** "Blocked: deleting files" in the thread: what was tried, why it never happens, and where the file went. */
export function BlockCard({ block }: { block: BlockData }) {
  const { t } = useI18n();
  const shell = useShell();
  const rule = isRule(block.rule) ? block.rule : "other";
  return (
    <Bubble from="assistant" className="chat-perm perm-block" role="group" aria-label={t(`block.title.${rule}`)} data-testid="block-card" data-rule={block.rule}>
      <div className="perm-block-head">
        <Pill tone="bad">
          <Icon name="shield" size={12} strokeWidth={2} />
          {t("block.badge")}
        </Pill>
        <span className="h2">{t(`block.title.${rule}`)}</span>
      </div>
      {block.target ? (
        <span className="p perm-block-target" data-testid="block-target">
          {t("block.wanted", { target: block.target })}
        </span>
      ) : null}
      <span className="p">{t(`block.why.${rule}`)}</span>
      {block.rule === "delete" ? (
        <div className="chat-perm-actions">
          <Button size="sm" onClick={() => shell.openSettings("trash")}>
            {t("block.viewTrash")}
          </Button>
        </div>
      ) : null}
    </Bubble>
  );
}
