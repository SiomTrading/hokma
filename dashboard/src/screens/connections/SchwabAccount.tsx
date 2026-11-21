import { useEffect, useState } from "react";
import { Button, IconButton, Loading, Modal } from "@/design";
import { useI18n } from "@/i18n";
import { connectionErrorText, schwabApi } from "./api";
import type { SchwabAccountEntry, SchwabAccountPanel, SchwabMoney, SchwabOrder, SchwabPosition } from "./types";

const DASH = "-";

type Format = (value: number | null | undefined, kind?: "money" | "number" | "percent") => string;

function makeFormat(locale: string): Format {
  const money = new Intl.NumberFormat(locale, { style: "currency", currency: "USD" });
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 4 });
  const percent = new Intl.NumberFormat(locale, { maximumFractionDigits: 2, signDisplay: "exceptZero" });
  return (value, kind = "money") => {
    if (value === null || value === undefined || Number.isNaN(value)) return DASH;
    return kind === "money" ? money.format(value) : kind === "percent" ? percent.format(value) + "%" : number.format(value);
  };
}

/** Margin accounts show equity, buying power and available funds; cash accounts show cash and cash available (5.7). Both show the start of day. */
function balanceRows(account: SchwabAccountEntry, t: (key: string) => string): Array<{ key: string; label: string; value: number | null | undefined }> {
  const now: SchwabMoney = account.currentBalances ?? {};
  const start: SchwabMoney = account.initialBalances ?? {};
  const cash = (account.type ?? "").toUpperCase() === "CASH";
  const lines = cash
    ? [
        { key: "totalCash", label: t("connections.schwab.bal.totalCash"), value: now.totalCash },
        { key: "cashAvailableForTrading", label: t("connections.schwab.bal.cashAvailable"), value: now.cashAvailableForTrading },
      ]
    : [
        { key: "equity", label: t("connections.schwab.bal.equity"), value: now.equity },
        { key: "buyingPower", label: t("connections.schwab.bal.buyingPower"), value: now.buyingPower },
        { key: "availableFunds", label: t("connections.schwab.bal.availableFunds"), value: now.availableFunds },
      ];
  return [...lines, { key: "liquidationValue", label: t("connections.schwab.bal.startOfDay"), value: start.liquidationValue }];
}

function ordersOf(account: SchwabAccountEntry, panel: SchwabAccountPanel): SchwabOrder[] {
  if (account.orders) return account.orders;
  const all = panel.orders ?? [];
  const tagged = all.filter((order) => order.account_ref !== undefined);
  // Orders that name their account go to it; orders that do not are shown with the only account there is.
  return tagged.length > 0 ? tagged.filter((order) => order.account_ref === account.account_ref) : panel.accounts.length === 1 ? all : [];
}

/** Panel "View account" (5.7): balances, positions and open orders of each account, masked. One line says whether agents trade in it (orders switch). There is no order button. */
export function SchwabAccount() {
  const { t, locale } = useI18n();
  const [panel, setPanel] = useState<SchwabAccountPanel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const fmt = makeFormat(locale);

  useEffect(() => {
    let alive = true;
    setError(null);
    schwabApi.account().then(
      (data) => {
        if (alive) setPanel(data);
      },
      (cause: unknown) => {
        if (alive) setError(connectionErrorText(cause));
      },
    );
    return () => {
      alive = false;
    };
  }, [tick]);

  return (
    <div className="conn-account" data-testid="schwab-account-panel">
      {panel ? (
        <p className="p conn-note" data-testid="schwab-readonly">
          {t(panel.orders_enabled === true ? "connections.schwab.trading" : "connections.schwab.readOnly")}
        </p>
      ) : null}
      {error ? (
        <div className="conn-failure">
          <p className="conn-error" role="alert" data-testid="schwab-account-error">
            {error}
          </p>
          <Button size="sm" onClick={() => setTick((n) => n + 1)}>
            {t("ui.error.retry")}
          </Button>
        </div>
      ) : panel === null ? (
        <Loading label={t("connections.loading")} />
      ) : (
        panel.accounts.map((account) => (
          <section key={account.account_ref} className="conn-account-card" aria-label={t("connections.schwab.account", { mask: account.mask })} data-testid={"schwab-account-" + account.account_ref}>
            <h3 className="h2 mono">{t("connections.schwab.account", { mask: account.mask })}</h3>

            <div data-testid="schwab-balances">
              <h4 className="lbl">{t("connections.schwab.balances")}</h4>
              <dl className="conn-balances">
                {balanceRows(account, t).map((row) => (
                  <div key={row.key} className="conn-balance">
                    <dt className="p">{row.label}</dt>
                    <dd className="mono">{fmt(row.value)}</dd>
                  </div>
                ))}
              </dl>
            </div>

            <div data-testid="schwab-positions">
              <h4 className="lbl">{t("connections.schwab.positions")}</h4>
              {(account.positions ?? []).length === 0 ? (
                <p className="p">{t("connections.schwab.none")}</p>
              ) : (
                <PositionsTable positions={account.positions ?? []} fmt={fmt} />
              )}
            </div>

            <div data-testid="schwab-orders">
              <h4 className="lbl">{t("connections.schwab.openOrders")}</h4>
              {ordersOf(account, panel).length === 0 ? <p className="p">{t("connections.schwab.none")}</p> : <OrdersTable orders={ordersOf(account, panel)} fmt={fmt} />}
            </div>
          </section>
        ))
      )}
    </div>
  );
}

function PositionsTable({ positions, fmt }: { positions: SchwabPosition[]; fmt: Format }) {
  const { t } = useI18n();
  return (
    <div className="conn-table-wrap">
      <table className="conn-table">
        <thead>
          <tr>
            <th scope="col">{t("connections.schwab.col.symbol")}</th>
            <th scope="col">{t("connections.schwab.col.type")}</th>
            <th scope="col">{t("connections.schwab.col.long")}</th>
            <th scope="col">{t("connections.schwab.col.short")}</th>
            <th scope="col">{t("connections.schwab.col.avgPrice")}</th>
            <th scope="col">{t("connections.schwab.col.marketValue")}</th>
            <th scope="col">{t("connections.schwab.col.dayPl")}</th>
            <th scope="col">{t("connections.schwab.col.dayPlPct")}</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position, index) => (
            <tr key={(position.instrument?.symbol ?? "") + index}>
              <th scope="row" className="mono">
                {position.instrument?.symbol ?? DASH}
              </th>
              <td>{position.instrument?.assetType ?? DASH}</td>
              <td className="mono">{fmt(position.longQuantity, "number")}</td>
              <td className="mono">{fmt(position.shortQuantity, "number")}</td>
              <td className="mono">{fmt(position.averagePrice)}</td>
              <td className="mono">{fmt(position.marketValue)}</td>
              <td className="mono">{fmt(position.currentDayProfitLoss)}</td>
              <td className="mono">{fmt(position.currentDayProfitLossPercentage, "percent")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTable({ orders, fmt }: { orders: SchwabOrder[]; fmt: Format }) {
  const { t, locale } = useI18n();
  const when = (value: string | undefined) => {
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" }).format(date) : DASH;
  };
  return (
    <div className="conn-table-wrap">
      <table className="conn-table">
        <thead>
          <tr>
            <th scope="col">{t("connections.schwab.col.symbol")}</th>
            <th scope="col">{t("connections.schwab.col.instruction")}</th>
            <th scope="col">{t("connections.schwab.col.quantity")}</th>
            <th scope="col">{t("connections.schwab.col.orderType")}</th>
            <th scope="col">{t("connections.schwab.col.price")}</th>
            <th scope="col">{t("connections.schwab.col.stopPrice")}</th>
            <th scope="col">{t("connections.schwab.col.duration")}</th>
            <th scope="col">{t("connections.schwab.col.status")}</th>
            <th scope="col">{t("connections.schwab.col.filled")}</th>
            <th scope="col">{t("connections.schwab.col.remaining")}</th>
            <th scope="col">{t("connections.schwab.col.entered")}</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order, index) => {
            const leg = order.orderLegCollection?.[0];
            return (
              <tr key={index}>
                <th scope="row" className="mono">
                  {order.orderLegCollection?.map((l) => l.instrument?.symbol).filter(Boolean).join(", ") || DASH}
                </th>
                <td>{leg?.instruction ?? DASH}</td>
                <td className="mono">{fmt(leg?.quantity, "number")}</td>
                <td>{order.orderType ?? DASH}</td>
                <td className="mono">{fmt(order.price)}</td>
                <td className="mono">{fmt(order.stopPrice)}</td>
                <td>{order.duration ?? DASH}</td>
                <td>{order.status ?? DASH}</td>
                <td className="mono">{fmt(order.filledQuantity, "number")}</td>
                <td className="mono">{fmt(order.remainingQuantity, "number")}</td>
                <td>{when(order.enteredTime)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** The panel in its own dialog, opened from the menu of the Schwab row. */
export function SchwabAccountDialog({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Modal label={t("connections.schwab.viewAccount")} onClose={onClose} className="conn-dialog">
      <div className="conn-dialog-body">
        <header className="conn-dialog-head">
          <h2 className="h1">{t("connections.schwab.viewAccount")}</h2>
          <IconButton icon="close" label={t("connections.add.close")} onClick={onClose} />
        </header>
        <SchwabAccount />
      </div>
    </Modal>
  );
}
