import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { DragEvent, KeyboardEvent } from "react";
import { cx } from "@/design";
import { useI18n } from "@/i18n";
import { dropAt, isFixedPanel, missingPanels, moveBy } from "./panels";
import type { DeskTab } from "./panels";

interface Props {
  order: DeskTab[];
  tab: DeskTab;
  tabKey: string;
  onTab: (tab: DeskTab) => void;
  /** The whole order after a drag or a key move. */
  onOrder: (order: DeskTab[]) => void;
  onAdd: (panel: DeskTab) => void;
  onRemove: (panel: DeskTab) => void;
  /** Panels that cannot be added now, each with the reason: they show in the menu disabled. */
  blocked?: Partial<Record<DeskTab, string>>;
}

/**
 * The tabs of a desk (SPEC-025, delivery 3): every tab moves by dragging it, or with Ctrl+Shift+Arrow Left or Right when it has the focus
 * (the move is announced once, "Rules, position 3 of 6"). The panels that can leave have an "x"; "+ Add" lists the ones that are missing.
 */
export function PanelTabs({ order, tab, tabKey, onTab, onOrder, onAdd, onRemove, blocked = {} }: Props) {
  const { t } = useI18n();
  const dragging = useRef<DeskTab | null>(null);
  const [over, setOver] = useState<{ id: DeskTab; before: boolean } | null>(null);
  const [note, setNote] = useState("");
  const [menu, setMenu] = useState(false);
  /** The tab that was moved by key: its button is rebuilt in the new place, so it gets the focus back there. */
  const refocus = useRef<DeskTab | null>(null);
  const missing = missingPanels(order);
  const name = (id: DeskTab) => t(`desks.tab.${id}`);

  useLayoutEffect(() => {
    if (refocus.current === null) return;
    document.getElementById(`${tabKey}-tab-${refocus.current}`)?.focus();
    refocus.current = null;
  }, [order, tabKey]);

  useEffect(() => {
    if (missing.length === 0) setMenu(false);
  }, [missing.length]);

  const onKey = (id: DeskTab, event: KeyboardEvent<HTMLButtonElement>) => {
    if (!(event.ctrlKey && event.shiftKey) || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
    event.preventDefault();
    const next = moveBy(order, id, event.key === "ArrowLeft" ? -1 : 1);
    if (next.join() === order.join()) return;
    refocus.current = id;
    setNote(t("desks.panels.moved", { name: name(id), pos: String(next.indexOf(id) + 1), total: String(next.length) }));
    onOrder(next);
  };

  const sideOf = (event: DragEvent<HTMLElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return event.clientX < box.left + box.width / 2;
  };

  const drop = (target: DeskTab, event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    const id = dragging.current;
    dragging.current = null;
    setOver(null);
    if (id === null || id === target) return;
    const next = dropAt(order, id, target, sideOf(event));
    if (next.join() === order.join()) return;
    setNote(t("desks.panels.moved", { name: name(id), pos: String(next.indexOf(id) + 1), total: String(next.length) }));
    onOrder(next);
  };

  return (
    <>
    <div className="ext-tabs desk-tabs" role="tablist" aria-label={t("desks.tabs.label")}>
      {order.map((id) => (
        <div
          key={id}
          role="presentation"
          className={cx("desk-tab", over?.id === id && (over.before ? "drop-before" : "drop-after"))}
          onDragOver={(event) => {
            if (dragging.current === null) return;
            event.preventDefault();
            const before = sideOf(event);
            if (over?.id !== id || over.before !== before) setOver({ id, before });
          }}
          onDrop={(event) => drop(id, event)}
        >
          <button
            type="button"
            role="tab"
            id={`${tabKey}-tab-${id}`}
            aria-selected={id === tab}
            aria-controls={`${tabKey}-panel`}
            aria-keyshortcuts="Control+Shift+ArrowLeft Control+Shift+ArrowRight"
            className="ext-tab"
            draggable
            onClick={() => onTab(id)}
            onKeyDown={(event) => onKey(id, event)}
            onDragStart={(event) => {
              dragging.current = id;
              event.dataTransfer?.setData("text/plain", id);
              if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
            }}
            onDragEnd={() => {
              dragging.current = null;
              setOver(null);
            }}
          >
            {name(id)}
          </button>
          {isFixedPanel(id) ? null : (
            <button type="button" className="desk-tab-x" aria-label={t("desks.panels.remove", { name: name(id) })} onClick={() => onRemove(id)}>
              ×
            </button>
          )}
        </div>
      ))}
      {missing.length > 0 ? (
        <div
          role="presentation"
          className="desk-tab-add"
          onKeyDown={(event) => {
            if (event.key === "Escape") setMenu(false);
          }}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setMenu(false);
          }}
        >
          <button type="button" className="ext-tab" aria-haspopup="true" aria-expanded={menu} onClick={() => setMenu(!menu)}>
            {t("desks.panels.add")}
          </button>
          {menu ? (
            <ul className="desk-tab-menu" aria-label={t("desks.panels.addLabel")}>
              {missing.map((id) => (
                <li key={id}>
                  <button
                    type="button"
                    disabled={blocked[id] !== undefined}
                    aria-disabled={blocked[id] !== undefined}
                    onClick={() => {
                      setMenu(false);
                      onAdd(id);
                    }}
                  >
                    <b>{name(id)}</b>
                    <span>{blocked[id] ?? t(`desks.panel.desc.${id}`)}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
    <span className="sr-only" role="status" aria-live="polite">
      {note}
    </span>
    </>
  );
}
