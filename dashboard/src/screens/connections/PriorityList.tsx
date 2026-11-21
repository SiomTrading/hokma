import { useState } from "react";
import { IconButton } from "@/design";
import { useI18n } from "@/i18n";
import type { ConnectionCard, UseKey } from "./types";

interface Props {
  use: UseKey;
  /** The connected sources of the use, in the order they are tried. */
  order: string[];
  cards: Map<string, ConnectionCard>;
  focused: boolean;
  onReorder: (use: UseKey, order: string[]) => void;
}

function move(order: string[], from: number, to: number): string[] {
  if (from === to || from < 0 || to < 0 || from >= order.length || to >= order.length) return order;
  const next = [...order];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item!);
  return next;
}

/** The connected providers of one use in the order they are tried. Drag a row, or use the arrow buttons. */
export function PriorityList({ use, order, cards, focused, onReorder }: Props) {
  const { t } = useI18n();
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const title = t("connections.use." + use);

  return (
    <section className={"conn-priority" + (focused ? " focus" : "")} aria-label={t("connections.priority.for", { class: title })} data-testid={"priority-" + use} data-focus={focused ? "true" : undefined}>
      <ol className="conn-order">
        {order.map((id, index) => {
          const name = cards.get(id)?.name ?? id;
          return (
            <li
              key={id}
              className={"conn-order-item" + (over === index && dragging !== null && dragging !== index ? " over" : "")}
              data-provider={id}
              draggable
              onDragStart={(event) => {
                setDragging(index);
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", id);
              }}
              onDragOver={(event) => {
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                setOver(index);
              }}
              onDragLeave={() => setOver((current) => (current === index ? null : current))}
              onDrop={(event) => {
                event.preventDefault();
                if (dragging !== null) onReorder(use, move(order, dragging, index));
                setDragging(null);
                setOver(null);
              }}
              onDragEnd={() => {
                setDragging(null);
                setOver(null);
              }}
            >
              <span className="mono conn-rank">{index + 1}</span>
              <span className="p conn-order-name">{name}</span>
              <IconButton icon="chevronUp" size={14} label={t("connections.priority.up", { name })} disabled={index === 0} onClick={() => onReorder(use, move(order, index, index - 1))} />
              <IconButton icon="chevronDown" size={14} label={t("connections.priority.down", { name })} disabled={index === order.length - 1} onClick={() => onReorder(use, move(order, index, index + 1))} />
            </li>
          );
        })}
      </ol>
    </section>
  );
}
