import { useEffect, useMemo, useState } from "react";
import { EmptyState, ErrorState, Input, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { strategyBankApi } from "@/api/strategyBank";
import type { BankRow } from "@/api/strategyBank";
import "./strategies.css";
import { StrategyDetail } from "./StrategyDetail";

export function originName(row: Pick<BankRow, "origin">, t: (key: string) => string): string {
  return row.origin.kind === "hokma" ? t("strategy.notice.hokma") : row.origin.desk_name ?? t("strategy.notice.desk");
}

/** The bank of strategies: what Hokma and the desks created, with its state, its version and where it comes from. Reading only; the detail has the two answers of the owner. */
export function StrategiesScreen() {
  const { t } = useI18n();
  const [rows, setRows] = useState<BankRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    strategyBankApi
      .list()
      .then((out) => live && setRows(Array.isArray(out?.rows) ? out.rows : []))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, []);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!rows) return [];
    if (!needle) return rows;
    return rows.filter((r) => [r.title, r.slug, originName(r, t), t(`strategies.status.${r.status}`), r.source.kind, r.source.reference ?? ""].some((v) => v.toLowerCase().includes(needle)));
  }, [rows, query, t]);

  return (
    <Pane as="section" aria-label={t("screen.strategies")} style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "auto" }}>
      <div className="str-page" data-testid="strategies-screen">
        {open !== null ? (
          <StrategyDetail bankId={open} onBack={() => setOpen(null)} />
        ) : (
          <>
            <h1 className="disp">{t("strategies.title")}</h1>
            <Input type="search" aria-label={t("strategies.search")} placeholder={t("strategies.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
            {failed ? (
              <ErrorState message={t("strategies.error")} />
            ) : rows === null ? (
              <Loading />
            ) : shown.length === 0 ? (
              <EmptyState title={rows.length === 0 ? t("strategies.empty") : t("strategies.noMatch")} />
            ) : (
              <table className="str-table">
                <thead>
                  <tr>
                    <th scope="col">{t("strategies.col.name")}</th>
                    <th scope="col">{t("strategies.col.origin")}</th>
                    <th scope="col">{t("strategies.col.status")}</th>
                    <th scope="col">{t("strategies.col.version")}</th>
                    <th scope="col">{t("strategies.col.source")}</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.bank_id} className="str-row" data-testid="strategy-row" tabIndex={0} onClick={() => setOpen(r.bank_id)} onKeyDown={(e) => (e.key === "Enter" ? setOpen(r.bank_id) : undefined)}>
                      <td>{r.title}</td>
                      <td>{originName(r, t)}</td>
                      <td>{t(`strategies.status.${r.status}`)}</td>
                      <td>v{r.version}</td>
                      <td>{[r.source.kind, r.source.reference].filter(Boolean).join(": ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </Pane>
  );
}
