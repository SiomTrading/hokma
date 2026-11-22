import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { installFakeSockets } from "@/test/fakeSocket";
import { FakeConnections } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { ConnectionsScreen } from "./ConnectionsScreen";

let backend: FakeConnections;

beforeEach(() => {
  backend = new FakeConnections();
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
  installFakeSockets();
  vi.stubGlobal("fetch", vi.fn(backend.handle));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the message when Alpaca is connected (SPEC-016 3.9)", () => {
  it("says once, with this exact text, that the free plan is limited, and closes with OK", async () => {
    expect(t("connections.alpaca.notice")).toBe("Alpaca's free plan has limited data and is not optimal for options.");
    renderWithApp(<ConnectionsScreen />);
    await userEvent.click(await screen.findByRole("button", { name: t("connections.add") }));
    await userEvent.click(within(await screen.findByTestId("add-list")).getByRole("button", { name: t("connections.add.choose"), description: "Alpaca Trading" }));
    await userEvent.type(screen.getByLabelText(t("connections.field.keyId")), "AKTESTKEYID12345");
    await userEvent.type(screen.getByLabelText(t("connections.field.secret")), "alpaca-secret-67890");
    await userEvent.click(screen.getByRole("button", { name: t("connections.add.testAndAdd") }));
    expect(await screen.findByTestId("alpaca-notice")).toHaveTextContent("Alpaca's free plan has limited data and is not optimal for options.");
    await userEvent.click(screen.getByRole("button", { name: "OK" }));
    expect(screen.queryByTestId("alpaca-notice")).not.toBeInTheDocument();
  });

  it("names the source Alpaca Trading and nothing else next to it", () => {
    for (const locale of ["en", "es"] as const) {
      setLocale(locale);
      expect(t("options.noConnection.any")).toContain("Alpaca Trading");
      expect(t("options.noConnection.any")).not.toContain("IEX");
    }
  });
});
