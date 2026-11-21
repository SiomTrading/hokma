import { useState } from "react";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { PriorityList } from "./PriorityList";
import type { ConnectionCard, UseKey, UseOrders } from "./types";
import { USES } from "./types";

interface Props {
  uses: UseOrders;
  cards: Map<string, ConnectionCard>;
  /** The use the chart link asked for (`class=equity`). */
  focusUse: UseKey | null;
  /** Sends the whole order of a use: `order` has every provider of it, the not connected ones after the connected ones. */
  onReorder: (use: UseKey, order: string[]) => void;
}

/** "Order of use": a row per kind of data that has at least one connected source, with only the connected ones in it. */
export function UseOrder({ uses, cards, focusUse, onReorder }: Props) {
  const { t } = useI18n();
  const [editing, setEditing] = useState<UseKey | null>(null);

  const rows = USES.map((use) => {
    const all = uses[use] ?? [];
    const connected = all.filter((id) => cards.get(id)?.added === true);
    return { use, all, connected };
  }).filter((row) => row.connected.length > 0);
  if (rows.length === 0) return null;

  return (
    <section className="conn-section" aria-label={t("connections.order.title")} data-testid="use-order">
      <h2 className="sec conn-group-title">{t("connections.order.title")}</h2>
      <p className="p">{t("connections.order.help")}</p>
      <ul className="conn-list">
        {rows.map(({ use, all, connected }) => {
          const labelId = "use-label-" + use;
          const change = (next: string[]) => {
            const rest = all.filter((id) => !next.includes(id));
            onReorder(use, [...next, ...rest]);
          };
          return (
            <li key={use} className="conn-use" data-testid={"use-" + use} data-focus={focusUse === use ? "true" : undefined}>
              <span className="lbl conn-use-label" id={labelId}>
                {t("connections.use." + use)}
              </span>
              {editing === use ? (
                <>
                  <PriorityList use={use} order={connected} cards={cards} focused={focusUse === use} onReorder={(_, next) => change(next)} />
                  <Button size="sm" variant="primary" onClick={() => setEditing(null)}>
                    {t("connections.order.done")}
                  </Button>
                </>
              ) : (
                <>
                  <ol className="conn-use-items" data-testid={"use-items-" + use}>
                    {connected.map((id, index) => (
                      <li key={id} data-provider={id}>
                        <span className="mono conn-rank">{index + 1}</span> {cards.get(id)?.name ?? id}
                      </li>
                    ))}
                  </ol>
                  {connected.length > 1 ? (
                    <Button size="sm" aria-describedby={labelId} onClick={() => setEditing(use)}>
                      {t("connections.order.change")}
                    </Button>
                  ) : null}
                </>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
