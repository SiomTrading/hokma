import { cx, Dot, Pill, Select } from "@/design";
import { useI18n } from "@/i18n";
import { formatCapital, formatPercent, formatResult, resultTone } from "./format";
import { DESK_TABS } from "./types";
import type { DeskSummary, DeskTab } from "./types";

interface Props {
  desk: DeskSummary;
  desks: DeskSummary[];
  tab: DeskTab;
  tabKey: string;
  onTab: (tab: DeskTab) => void;
  onPickDesk: (id: number) => void;
}

/** Top bar of a desk: desk selector, Simulation pill, the five tabs, capital, result of the day and drawdown. */
export function DeskBar({ desk, desks, tab, tabKey, onTab, onPickDesk }: Props) {
  const { t, locale } = useI18n();
  const paused = desk.status === "paused";
  return (
    <header className="pane desks-hdr desk-bar">
      <h1 className="sr-only">{desk.name}</h1>
      <span className="desk-pick">
        <Dot color={paused ? "var(--warn)" : "var(--ok)"} />
        <Select aria-label={t("desks.bar.pick")} value={String(desk.id)} onChange={(e) => onPickDesk(Number(e.target.value))}>
          {desks.map((item) => (
            <option key={item.id} value={String(item.id)}>
              {item.name}
            </option>
          ))}
        </Select>
      </span>
      <Pill tone="ok">{t("desks.mode.simulation")}</Pill>
      <div className="ext-tabs" role="tablist" aria-label={t("desks.tabs.label")}>
        {DESK_TABS.map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            id={`${tabKey}-tab-${id}`}
            aria-selected={id === tab}
            aria-controls={`${tabKey}-panel`}
            className="ext-tab"
            onClick={() => onTab(id)}
          >
            {t(`desks.tab.${id}`)}
          </button>
        ))}
      </div>
      <div className="desk-bar-stats">
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.capital")}</span>
          <b className="mono">{formatCapital(desk.capital, locale)}</b>
        </div>
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.today")}</span>
          <b className={cx("mono", resultTone(desk.today))}>{formatResult(desk.today, locale)}</b>
        </div>
        <div className="desk-stat">
          <span className="lbl">{t("desks.stat.drawdown")}</span>
          <b className="mono">{desk.drawdown === null ? t("desks.stat.none") : formatPercent(desk.drawdown, locale)}</b>
        </div>
        <button type="button" className="pill mono desk-palette" disabled title={t("desks.bar.paletteSoon")} aria-label={t("desks.bar.palette")}>
          ⌕ Ctrl K
        </button>
      </div>
    </header>
  );
}
