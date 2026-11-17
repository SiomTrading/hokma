import { useEffect, useState } from "react";
import { Bubble, Button, Icon, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import "./permissions.css";
import { permissionsApi } from "./api";
import { isRule } from "./types";
import type { TrashItem } from "./types";

export interface BlockData {
  id: number | string;
  rule: string;
  tool: string;
  target: string;
  at?: string;
}

/** How long after a block a Trash move still counts as the agent's follow-up to it. */
const FOLLOW_UP_MS = 10 * 60 * 1000;

function stamp(value: string): number {
  const iso = value.includes("T") ? value : value.replace(" ", "T");
  return Date.parse(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
}

/** Trash items that appeared right after the block: the proof that something really went to the Trash. */
export function relatedTrash(items: TrashItem[], at: string | undefined): TrashItem[] {
  const from = at ? stamp(at) : NaN;
  if (Number.isNaN(from)) return [];
  return items.filter((item) => {
    const moved = stamp(item.moved_at);
    return item.present && moved >= from - 5000 && moved <= from + FOLLOW_UP_MS;
  });
}

/** "Blocked: deleting files" in the thread: what was tried, why it never happens, and where the file went. */
export function BlockCard({ block }: { block: BlockData }) {
  const { t } = useI18n();
  const shell = useShell();
  const rule = isRule(block.rule) ? block.rule : "other";
  const [inTrash, setInTrash] = useState(false);
  useEffect(() => {
    if (block.rule !== "delete") return;
    let alive = true;
    permissionsApi.trash().then(
      (list) => alive && setInTrash(relatedTrash(list.items, block.at).length > 0),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [block.rule, block.at]);
  const why = block.rule === "delete" && !inTrash ? t("block.why.deleteBlocked") : t(`block.why.${rule}`);
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
      <span className="p">{why}</span>
      {block.rule === "delete" && inTrash ? (
        <div className="chat-perm-actions">
          <Button size="sm" onClick={() => shell.openSettings("trash")}>
            {t("block.viewTrash")}
          </Button>
        </div>
      ) : null}
    </Bubble>
  );
}
