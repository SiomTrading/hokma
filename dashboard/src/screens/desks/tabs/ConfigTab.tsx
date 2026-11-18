import { useCallback, useEffect, useRef, useState } from "react";
import { Button, cx, ErrorState, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { deskConfigApi, failText } from "../configApi";
import { CONFIG_SECTIONS } from "../configTypes";
import type { ConfigSection, DeskDetail } from "../configTypes";
import { DeleteDeskModal } from "../DeleteDeskModal";
import { DeskEmptyState } from "../EmptyState";
import { ConnectionsSection } from "./config/ConnectionsSection";
import { ObjectiveSection } from "./config/ObjectiveSection";
import type { TabProps } from "./types";

/**
 * Settings of a desk. Two real sections (objective and capital, connections) and two empty ones (memory and linked
 * desks, trading accounts). The assets of the desk are managed in the Terminal, in the watchlist. The detail is
 * loaded once per desk.
 */
export function ConfigTab({ desk }: TabProps) {
  const { t } = useI18n();
  const [section, setSection] = useState<ConfigSection>("objective");
  const [detail, setDetail] = useState<DeskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const ticket = useRef(0);

  const load = useCallback(() => {
    const mine = (ticket.current += 1);
    setError(null);
    deskConfigApi
      .detail(desk.id)
      .then((value) => {
        if (mine === ticket.current) setDetail(value);
      })
      .catch((cause: unknown) => {
        if (mine === ticket.current) setError(failText(cause, t("desks.cfg.error.load")));
      });
  }, [desk.id, t]);
  useEffect(() => {
    setDetail(null);
    load();
    return () => {
      ticket.current += 1;
    };
  }, [load]);

  const needsDetail = section === "objective";
  return (
    <div className="desk-cfg">
      <nav className="pane desk-cfg-nav" aria-label={t("desks.cfg.nav")}>
        {CONFIG_SECTIONS.map((id) => (
          <button
            key={id}
            type="button"
            className={cx("nav", id === section && "on")}
            aria-current={id === section ? "page" : undefined}
            onClick={() => setSection(id)}
          >
            <span className="desk-cfg-label">{t(`desks.cfg.section.${id}`)}</span>
            {id === "accounts" ? <Pill className="desk-cfg-later">{t("desks.cfg.sim")}</Pill> : null}
          </button>
        ))}
      </nav>
      <div className="pane desk-cfg-body" role="region" aria-label={t(`desks.cfg.section.${section}`)}>
        {needsDetail && error ? (
          <ErrorState message={error} onRetry={load} />
        ) : needsDetail && detail === null ? (
          <Loading label={t("desks.cfg.loading")} />
        ) : null}
        {section === "objective" && detail ? <ObjectiveSection key={desk.id} deskId={desk.id} detail={detail} onSaved={setDetail} /> : null}
        {section === "connections" ? <ConnectionsSection key={desk.id} deskId={desk.id} /> : null}
        {section === "memory" ? (
          <DeskEmptyState icon="library" title={t("desks.cfg.memory.title")} text={t("desks.cfg.memory.text")} />
        ) : null}
        {section === "accounts" ? (
          <DeskEmptyState icon="shield" title={t("desks.cfg.accounts.title")} text={t("desks.cfg.accounts.text")} />
        ) : null}
        <section className="desk-danger" aria-label={t("desks.delete.zone")}>
          <h3 className="lbl">{t("desks.delete.zone")}</h3>
          <p className="p">{t("desks.delete.zoneText")}</p>
          <Button variant="danger" onClick={() => setDeleting(true)}>
            {t("desks.delete.button")}
          </Button>
        </section>
      </div>
      {deleting ? <DeleteDeskModal desk={desk} onClose={() => setDeleting(false)} /> : null}
    </div>
  );
}
