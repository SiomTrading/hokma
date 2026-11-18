import { useRef, useState } from "react";
import { cx, Menu, MenuItem, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { DeleteDeskModal } from "./DeleteDeskModal";
import { formatCapital, formatPercent, formatResult, resultTone } from "./format";
import type { DeskSummary } from "./types";

interface Props {
  desk: DeskSummary;
  onOpen: () => void;
}

/** One desk in the Fund: name, focus, state, capital, result of the day, drawdown and agents working. */
export function DeskCard({ desk, onOpen }: Props) {
  const { t, locale } = useI18n();
  const [menu, setMenu] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const paused = desk.status === "paused";
  const today = formatResult(desk.today, locale);
  return (
    <div className="desk-card-wrap">
    <button type="button" className="pane desk-card" onClick={onOpen} aria-label={t("desks.card.open", { name: desk.name })}>
      <div className="desk-card-top">
        <div className="desk-card-id">
          <span className="desk-card-name">{desk.name}</span>
          {desk.focus ? <span className="desk-card-focus">{desk.focus}</span> : null}
        </div>
        <Pill tone={paused ? "warn" : "ok"}>{t(paused ? "desks.status.paused" : "desks.status.active")}</Pill>
      </div>
      <div className="desk-card-stats">
        <div>
          <div className="lbl">{t("desks.stat.capital")}</div>
          <div className="mono">{formatCapital(desk.capital, locale)}</div>
        </div>
        <div>
          <div className="lbl">{t("desks.stat.today")}</div>
          <div className={cx("mono", resultTone(desk.today))}>{today}</div>
        </div>
        <div>
          <div className="lbl">{t("desks.stat.drawdown")}</div>
          <div className="mono">{desk.drawdown === null ? t("desks.stat.none") : formatPercent(desk.drawdown, locale)}</div>
        </div>
      </div>
      <div className="desk-card-foot">
        <span>{t("desks.card.working", { n: String(desk.agents_working), total: String(desk.agents_total) })}</span>
        <span className="desk-card-pills">
          {desk.budget_state !== "ok" ? (
            <Pill tone={desk.budget_state === "exhausted" ? "bad" : "warn"}>{t(`desks.budget.${desk.budget_state}`)}</Pill>
          ) : null}
          <Pill>{t("desks.mode.simulation")}</Pill>
        </span>
      </div>
    </button>
      <button ref={trigger} type="button" className="desk-card-more" aria-label={t("desks.card.options", { name: desk.name })} aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
        ···
      </button>
      <Menu open={menu} onClose={() => setMenu(false)} label={t("desks.card.options", { name: desk.name })} triggerRef={trigger} className="desk-card-menu">
        <MenuItem onClick={() => { setMenu(false); setDeleting(true); }}>{t("desks.delete.button")}</MenuItem>
      </Menu>
      {deleting ? <DeleteDeskModal desk={desk} onClose={() => setDeleting(false)} /> : null}
    </div>
  );
}
