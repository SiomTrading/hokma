import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setLocale } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { BrokerAlertBand } from "./BrokerAlertBand";

const LOST = { state: "lost" as const, source: "Schwab", message: "Connection with Schwab was lost. Data is not updating." };
const BACK = { state: "recovered" as const, source: "Schwab", message: "Connection with Schwab recovered at 10:01." };

describe("BrokerAlertBand", () => {
  it("shows nothing without an alert", () => {
    setLocale("en");
    renderWithApp(<BrokerAlertBand alert={null} assetClass="equity" />);
    expect(screen.queryByTestId("broker-alert")).not.toBeInTheDocument();
  });

  it("is red with a link to Connections while the broker is lost", () => {
    setLocale("en");
    renderWithApp(<BrokerAlertBand alert={LOST} assetClass="equity" />);
    expect(screen.getByTestId("broker-alert")).toHaveAttribute("data-state", "lost");
    expect(screen.getByTestId("broker-alert")).toHaveTextContent(LOST.message);
    expect(screen.getByTestId("connect-link")).toBeInTheDocument();
  });

  it("is green and has no link when it is back", () => {
    setLocale("en");
    renderWithApp(<BrokerAlertBand alert={BACK} assetClass="equity" />);
    expect(screen.getByTestId("broker-alert")).toHaveAttribute("data-state", "recovered");
    expect(screen.queryByTestId("connect-link")).not.toBeInTheDocument();
  });
});
