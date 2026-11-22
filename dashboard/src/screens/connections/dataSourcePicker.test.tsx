import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearToken, setToken } from "@/api";
import { setLocale } from "@/i18n";
import { card } from "@/test/connectionsBackend";
import { TEST_TOKEN, renderWithApp } from "@/test/utils";
import { DataSourcePicker } from "./DataSourcePicker";

const BROKERS = [
  card({ id: "schwab", name: "Schwab", role: "broker", added: true }),
  card({ id: "ibkr", name: "Interactive Brokers", role: "broker", added: true }),
  card({ id: "alpaca", name: "Alpaca Trading", role: "free", added: true }),
];

beforeEach(() => {
  clearToken();
  setToken(TEST_TOKEN);
  setLocale("en");
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Live data source", () => {
  it("shows nothing with one broker or none", () => {
    renderWithApp(<DataSourcePicker cards={BROKERS.slice(0, 1)} chosen={null} onChosen={() => undefined} onReload={() => undefined} />);
    expect(screen.queryByTestId("data-source")).not.toBeInTheDocument();
  });

  it("with two brokers offers only the brokers, marks the first when none is chosen and sends the choice", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ data_source: "ibkr" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const chosen: string[] = [];
    renderWithApp(<DataSourcePicker cards={BROKERS} chosen={null} onChosen={(id) => chosen.push(id)} onReload={() => undefined} />);
    expect(screen.getAllByRole("radio")).toHaveLength(2);                    // Alpaca is not a choice
    expect(screen.getByRole("radio", { name: "Schwab" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Interactive Brokers" }));
    await vi.waitFor(() => expect(chosen).toEqual(["ibkr"]));
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toContain("/api/connections/data-source");
    expect(JSON.parse(String(call[1].body))).toEqual({ provider: "ibkr" });
  });
});
