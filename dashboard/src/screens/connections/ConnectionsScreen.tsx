import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorState, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { connectionErrorText, connectionsApi } from "./api";
import "./connections.css";
import { useFocus } from "./focus";
import { PriorityList } from "./PriorityList";
import { ProviderCard } from "./ProviderCard";
import type { AssetClass, ConnectionCard } from "./types";
import { useConnections } from "./useConnections";

/** How the cards are laid out: one section per kind of data. Every provider appears once, in its section. */
const SECTIONS: ReadonlyArray<{ id: string; titleKey: string; providers: readonly string[]; order: AssetClass }> = [
  { id: "crypto", titleKey: "connections.group.crypto", providers: ["coinbase"], order: "crypto" },
  { id: "equity", titleKey: "connections.group.equity", providers: ["alpaca"], order: "equity" },
  { id: "mixed", titleKey: "connections.group.mixed", providers: ["finnhub"], order: "equity" },
  { id: "future", titleKey: "connections.group.future", providers: ["siomtrading_futures", "databento", "massive"], order: "future" },
];

export function ConnectionsScreen() {
  const { t } = useI18n();
  const state = useConnections();
  const focus = useFocus();
  const [priorityError, setPriorityError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const { cards, groups, setGroups, reload } = state;

  const byId = useMemo(() => new Map(cards.map((card) => [card.id, card])), [cards]);
  const sections = useMemo(() => {
    const placed = new Set(SECTIONS.flatMap((section) => section.providers));
    const known = SECTIONS.map((section) => {
      const list = section.providers.map((id) => byId.get(id)).filter((card): card is ConnectionCard => !!card);
      // The futures section is also the futures priority: the card on top is the one tried first.
      if (section.id === "future") list.sort((a, b) => (a.priority.future ?? 99) - (b.priority.future ?? 99));
      return { id: section.id, title: t(section.titleKey), cards: list };
    });
    const extra = cards.filter((card) => !placed.has(card.id));
    return extra.length ? [...known, { id: "more", title: t("connections.group.more"), cards: extra }] : known;
  }, [byId, cards, t]);

  const focusClass = focus.assetClass;
  useEffect(() => {
    if (!focusClass || state.loading) return;
    const target = rootRef.current?.querySelector('[data-focus="true"]');
    target?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [focus.nonce, focusClass, state.loading]);

  const reorder = useCallback(
    (assetClass: AssetClass, order: string[]) => {
      const before = groups;
      setGroups({ ...groups, [assetClass]: order });
      setPriorityError(null);
      connectionsApi.setPriority(assetClass, order).then(
        (answer) => {
          setGroups(answer.groups);
          reload();
        },
        (cause: unknown) => {
          setGroups(before);
          setPriorityError(connectionErrorText(cause));
        },
      );
    },
    [groups, reload, setGroups],
  );

  const patch = state.patchCard;
  const orderedClasses = (Object.keys(groups) as AssetClass[]).filter((cls) => (groups[cls]?.length ?? 0) > 1);

  return (
    <Pane as="section" aria-label={t("screen.connections")} style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "auto" }}>
      <div ref={rootRef} className="conn-page">
        <div className="conn-intro">
          <h1 className="disp">{t("connections.title")}</h1>
          <p className="p">{t("connections.intro")}</p>
        </div>

        {!state.storeAvailable ? (
          <p className="conn-banner" role="alert" data-testid="store-unavailable">
            {t("connections.store.unavailable")}
          </p>
        ) : null}

        {state.loading ? (
          <Loading label={t("connections.loading")} />
        ) : state.error && cards.length === 0 ? (
          <ErrorState title={t("connections.error.title")} message={state.error} onRetry={reload} />
        ) : (
          <>
            <div className="conn-groups">
            {sections.map((section) => (
              <section key={section.id} className={"conn-group" + (section.cards.length > 1 ? " wide" : "")} aria-label={section.title} data-group={section.id}>
                <h2 className="sec conn-group-title">{section.title}</h2>
                <div className="conn-grid">
                  {section.cards.map((card) => (
                    <ProviderCard
                      key={card.id}
                      card={card}
                      storeAvailable={state.storeAvailable}
                      focused={focusClass !== null && card.asset_classes.includes(focusClass)}
                      onChange={patch}
                      onStoreMissing={() => state.setStoreAvailable(false)}
                      onReload={reload}
                    />
                  ))}
                </div>
              </section>
            ))}
            </div>

            {orderedClasses.length > 0 ? (
              <section className="conn-group" aria-label={t("connections.priority.title")} data-group="priority">
                <h2 className="sec conn-group-title">{t("connections.priority.title")}</h2>
                <p className="p">{t("connections.priority.help")}</p>
                {priorityError ? (
                  <p className="conn-error" role="alert">
                    {priorityError}
                  </p>
                ) : null}
                <div className="conn-grid">
                  {orderedClasses.map((cls) => (
                    <PriorityList key={cls} assetClass={cls} order={groups[cls] ?? []} cards={byId} focused={focusClass === cls} onReorder={reorder} />
                  ))}
                </div>
              </section>
            ) : null}
          </>
        )}
      </div>
    </Pane>
  );
}
