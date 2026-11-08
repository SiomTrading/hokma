import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ShellProvider, tabKeyFor, useShell } from "./store";
import type { ShellApi } from "./store";

let api: ShellApi;

function Probe() {
  api = useShell();
  return (
    <>
      <output aria-label="tabs">{api.tabs.map((tab) => `${tab.key}|${JSON.stringify(tab.params)}|${tab.title ?? ""}`).join(" ; ")}</output>
      <output aria-label="active">{api.activeKey ?? ""}</output>
    </>
  );
}

const mount = () => render(<ShellProvider><Probe /></ShellProvider>);
const tabs = () => screen.getByRole("status", { name: "tabs" }).textContent ?? "";
const active = () => screen.getByRole("status", { name: "active" }).textContent ?? "";

describe("shell: setTabParams (T-11, store side)", () => {
  it("changes the params, the key and the title of an open tab, and keeps it active", () => {
    mount();
    act(() => api.openScreen("chart", { symbol: "NQ=F" }));
    const oldKey = tabKeyFor("chart", { symbol: "NQ=F" });
    expect(active()).toBe(oldKey);
    let newKey: string | null = null;
    act(() => {
      newKey = api.setTabParams(oldKey, { symbol: "AAPL" });
      api.setTabTitle(tabKeyFor("chart", { symbol: "AAPL" }), "AAPL");
    });
    expect(newKey).toBe("chart:symbol=AAPL");
    expect(active()).toBe("chart:symbol=AAPL");
    expect(tabs()).toContain('chart:symbol=AAPL|{"symbol":"AAPL"}|AAPL');
    expect(tabs()).not.toContain("NQ=F");
    // Opening the same params again finds the same tab instead of a new one.
    act(() => api.openScreen("chart", { symbol: "AAPL" }));
    expect(api.tabs.filter((tab) => tab.screen === "chart")).toHaveLength(1);
  });

  it("keeps the order of the tabs and does not move the focus when the tab was not active", () => {
    mount();
    act(() => api.openScreen("chart", { symbol: "NQ=F" }));
    act(() => api.activateTab("chat"));
    act(() => {
      api.setTabParams("chart:symbol=NQ=F", { symbol: "ES=F" });
    });
    expect(active()).toBe("chat");
    expect(api.tabs.map((tab) => tab.key)).toEqual(["chat", "panel", "chart:symbol=ES=F"]);
  });

  it("merges into the tab that already has those params instead of making two", () => {
    mount();
    act(() => api.openScreen("chart", { symbol: "AAPL" }));
    act(() => api.openScreen("chart", { symbol: "NQ=F" }));
    let key: string | null = null;
    act(() => {
      key = api.setTabParams("chart:symbol=NQ=F", { symbol: "AAPL" });
    });
    expect(key).toBe("chart:symbol=AAPL");
    expect(api.tabs.map((tab) => tab.key)).toEqual(["chat", "panel", "chart:symbol=AAPL"]);
    expect(active()).toBe("chart:symbol=AAPL");
  });

  it("does nothing for a tab that is not open", () => {
    mount();
    const before = tabs();
    let key: string | null = "x";
    act(() => {
      key = api.setTabParams("chart:symbol=ZZZ", { symbol: "AAPL" });
    });
    expect(key).toBeNull();
    expect(tabs()).toBe(before);
  });

  it("survives reloading the app: the new params, title and active tab come back", () => {
    const first = mount();
    act(() => api.openScreen("chart", { symbol: "NQ=F" }));
    act(() => {
      api.setTabParams("chart:symbol=NQ=F", { symbol: "BTC-USD" });
      api.setTabTitle("chart:symbol=BTC-USD", "BTC-USD");
    });
    first.unmount();
    mount();
    expect(tabs()).toContain('chart:symbol=BTC-USD|{"symbol":"BTC-USD"}|BTC-USD');
    expect(active()).toBe("chart:symbol=BTC-USD");
  });
});
