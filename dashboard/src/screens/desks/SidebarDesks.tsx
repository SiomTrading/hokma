import { cx, NavItem } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { formatCapital, formatResult, resultTone } from "./format";
import { useDesks } from "./store";
import "./desks.css";

/** The desks under "Mesas de dinero" in the menu: one row per desk with its capital and the result of the day. */
export function SidebarDesks() {
  const { t, locale } = useI18n();
  const shell = useShell();
  const { desks } = useDesks();
  const active = shell.tabs.find((tab) => tab.key === shell.activeKey);
  const activeDesk = active?.screen === "desks" ? active.params.desk : undefined;

  if (desks.length === 0) return null;
  return (
    <div className="desk-list" role="group" aria-label={t("desks.side.list")}>
      {desks.map((desk) => {
        const capital = formatCapital(desk.capital, locale);
        const today = formatResult(desk.today, locale);
        return (
          <button
            key={desk.id}
            type="button"
            className={cx("nav", "desk-link", activeDesk === String(desk.id) && "on")}
            aria-label={t("desks.side.row", { name: desk.name, capital, today })}
            title={desk.name}
            aria-current={activeDesk === String(desk.id) ? "page" : undefined}
            onClick={() => shell.openScreen("desks", { desk: String(desk.id) })}
          >
            <span className="desk-initial" aria-hidden="true">
              {desk.name.trim().slice(0, 2).toUpperCase()}
            </span>
            <span className={cx("dot", desk.status === "paused" ? "desk-dot-paused" : "desk-dot-active")} aria-hidden="true" />
            <span className="desk-link-text">
              <span className="desk-link-name">{desk.name}</span>
              <span className="mono desk-link-sub">
                {capital} · <span className={resultTone(desk.today)}>{today}</span>
              </span>
            </span>
          </button>
        );
      })}
      <NavItem className="desk-new" icon="plus" iconSize={14} label={t("desks.side.new")} title={t("desks.side.new")} onClick={() => shell.openScreen("desks", { new: "1" })} />
    </div>
  );
}
