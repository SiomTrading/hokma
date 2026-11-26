import { cleanup, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { getCatalog, setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { PnlCell, readTime } from "./PnlCell";
import type { PnlView } from "./pnlApi";

const LAST = { account_id: 1, equity_now: 24966, equity_start: 25000, day_pnl: -34, positions: [], as_of: "2026-11-25T15:00:05Z", source: "schwab" };
const view = (patch: Partial<PnlView>): PnlView => ({ state: "ok", source: "schwab", since: null, last: LAST, ...patch });

function show(v: PnlView | undefined) {
  return renderWithApp(<PnlCell view={v} currency="USD" />);
}

beforeEach(() => setLocale("es"));
afterEach(cleanup);

describe("the day P&L of an account that the broker gives", () => {
  it("shows the number of the broker with the hour of the last reading and the source", () => {
    show(view({}));
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent("-34,00 USD");
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent(t("desks.accounts.pnl.source.broker", { broker: "Schwab" }));
    expect(screen.getByTestId("pnl-ok").textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
  });

  it("names the broker that gave the number, in both languages, and the simulated one says simulated", () => {
    show(view({ source: "alpaca" }));
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent("fuente: Alpaca");
    cleanup();
    show(view({ source: "sim" }));
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent(t("desks.accounts.pnl.source.sim"));
    cleanup();
    setLocale("en");
    show(view({}));
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent("source: Schwab");
  });

  it("keeps the name of the broker when there is no data, and names the broker that has no reader of the loss", () => {
    show(view({ state: "no_data", source: "alpaca", since: LAST.as_of, last: { ...LAST, source: "alpaca" } }));
    expect(screen.getByTestId("pnl-provider")).toHaveTextContent("Alpaca");
    cleanup();
    show(view({ state: "unavailable", last: null, source: "ibkr" }));
    expect(screen.getByTestId("pnl-unavailable")).toHaveTextContent("IBKR");
  });

  it("says there is no data from the broker, since when, and keeps the last number only with its hour", () => {
    show(view({ state: "no_data", since: LAST.as_of }));
    const cell = screen.getByTestId("pnl-no-data");
    const time = readTime(LAST.as_of, "es");
    expect(cell).toHaveTextContent(t("desks.accounts.pnl.noData", { time }));
    expect(cell).toHaveTextContent(t("desks.accounts.pnl.lastKnown", { value: "-34,00 USD", time }));
  });

  it("says it is reading, and says the freeze is not available when the broker has no reader", () => {
    show(undefined);
    expect(screen.getByTestId("pnl-pending")).toHaveTextContent(t("desks.accounts.pnl.pending"));
    cleanup();
    show(view({ state: "unavailable", last: null, source: "ninjatrader" }));
    expect(screen.getByTestId("pnl-unavailable")).toHaveTextContent(t("desks.accounts.pnl.unavailable"));
  });

  it("is in both languages and never shows a made up zero when the broker gave no number", () => {
    expect(getCatalog("en")["desks.accounts.pnl.noData"]).toContain("broker");
    expect(getCatalog("en")["desks.accounts.pnl.source.broker"]).toContain("{broker}");
    expect(getCatalog("es")["desks.accounts.pnl.source.broker"]).not.toBe(getCatalog("en")["desks.accounts.pnl.source.broker"]);
    expect(getCatalog("es")["desks.accounts.pnl.noData"]).not.toBe(getCatalog("en")["desks.accounts.pnl.noData"]);
    show(view({ last: { ...LAST, day_pnl: null } }));
    expect(screen.getByTestId("pnl-ok")).not.toHaveTextContent("0,00");
    expect(screen.getByTestId("pnl-ok")).toHaveTextContent(t("desks.accounts.unavailable"));
  });
});
