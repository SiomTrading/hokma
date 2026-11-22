import { useState } from "react";
import { useI18n } from "@/i18n";
import { connectionErrorText, connectionsApi } from "./api";
import type { ConnectionCard } from "./types";

interface Props {
  cards: ConnectionCard[];
  chosen: string | null;
  onChosen: (id: string) => void;
  onReload: () => void;
}

/** "Live data source" (SPEC-016 3.6): with two brokers connected only one gives the data, never both at once. It shows only then. */
export function DataSourcePicker({ cards, chosen, onChosen, onReload }: Props) {
  const { t } = useI18n();
  const [error, setError] = useState<string | null>(null);
  const brokers = cards.filter((card) => card.role === "broker" && card.added);
  if (brokers.length < 2) return null;
  const current = brokers.some((card) => card.id === chosen) ? chosen : brokers[0]!.id;
  const choose = (id: string) => {
    setError(null);
    connectionsApi.setDataSource(id).then(
      () => {
        onChosen(id);
        onReload();
      },
      (cause: unknown) => setError(connectionErrorText(cause)),
    );
  };
  return (
    <section className="conn-source" aria-label={t("connections.source.title")} data-testid="data-source">
      <h2 className="h2">{t("connections.source.title")}</h2>
      <p className="p">{t("connections.source.note")}</p>
      <div role="radiogroup" aria-label={t("connections.source.title")} className="conn-actions">
        {brokers.map((card) => (
          <label key={card.id} className="conn-radio">
            <input type="radio" name="data-source" value={card.id} checked={current === card.id} onChange={() => choose(card.id)} /> {card.name}
          </label>
        ))}
      </div>
      {error ? (
        <p className="conn-error" role="alert" data-testid="data-source-error">
          {error}
        </p>
      ) : null}
    </section>
  );
}
