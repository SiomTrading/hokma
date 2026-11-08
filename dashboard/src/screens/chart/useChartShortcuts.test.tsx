import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { chartShortcutOf, useChartShortcuts } from "./useChartShortcuts";

const calls: string[] = [];
const outside = vi.fn();

function Page() {
  const body = useRef<HTMLDivElement>(null);
  useChartShortcuts(body, {
    onSearch: (initial) => calls.push(`search:${initial}`),
    onInterval: (initial) => calls.push(`interval:${initial}`),
  });
  return (
    <div onKeyDown={outside}>
      <div ref={body} tabIndex={0} data-testid="chart-body">
        <button type="button">zoom</button>
        <input aria-label="inside field" />
      </div>
      <textarea aria-label="chat composer" />
      <div className="xterm">
        <textarea aria-label="terminal input" className="xterm-helper-textarea" />
      </div>
      <div tabIndex={0} data-testid="elsewhere">
        elsewhere
      </div>
    </div>
  );
}

beforeEach(() => {
  calls.length = 0;
  outside.mockClear();
});

describe("chart keys (T-12)", () => {
  it("a letter or ^ opens the search with it, a digit opens the interval box with it", async () => {
    render(<Page />);
    screen.getByTestId("chart-body").focus();
    await userEvent.keyboard("n");
    await userEvent.keyboard("Q");
    await userEvent.keyboard("^");
    await userEvent.keyboard("7");
    expect(calls).toEqual(["search:n", "search:Q", "search:^", "interval:7"]);
    expect(outside).not.toHaveBeenCalled(); // handled keys stop at the chart
  });

  it("Ctrl+K and Cmd+K open the search empty", async () => {
    render(<Page />);
    screen.getByTestId("chart-body").focus();
    await userEvent.keyboard("{Control>}k{/Control}");
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(calls).toEqual(["search:", "search:"]);
  });

  it("works from any element inside the chart body that is not a field", async () => {
    render(<Page />);
    screen.getByRole("button", { name: "zoom" }).focus();
    await userEvent.keyboard("a");
    expect(calls).toEqual(["search:a"]);
  });

  it("leaves other keys and other modifiers alone (Ctrl+C is not taken)", async () => {
    render(<Page />);
    screen.getByTestId("chart-body").focus();
    await userEvent.keyboard("{Control>}c{/Control}");
    await userEvent.keyboard("{Alt>}a{/Alt}");
    await userEvent.keyboard("{Control>}{Shift>}k{/Shift}{/Control}");
    await userEvent.keyboard("{Enter}{ArrowLeft} -");
    expect(calls).toEqual([]);
    expect(outside).toHaveBeenCalled();
  });

  it("typing in a field, in the chat or in the terminal opens nothing", async () => {
    render(<Page />);
    await userEvent.type(screen.getByRole("textbox", { name: "inside field" }), "abc7");
    await userEvent.type(screen.getByRole("textbox", { name: "chat composer" }), "nq 5");
    await userEvent.type(screen.getByRole("textbox", { name: "terminal input" }), "ls 1");
    screen.getByRole("textbox", { name: "chat composer" }).focus();
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(calls).toEqual([]);
  });

  it("does nothing while the focus is outside the chart", async () => {
    render(<Page />);
    screen.getByTestId("elsewhere").focus();
    await userEvent.keyboard("a7");
    expect(calls).toEqual([]);
  });

  it("maps key presses to shortcuts", () => {
    const key = (k: string, mods: Partial<Record<"ctrlKey" | "metaKey" | "altKey" | "shiftKey", boolean>> = {}) =>
      chartShortcutOf({ key: k, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...mods });
    expect(key("a")).toEqual({ kind: "search", initial: "a" });
    expect(key("A", { shiftKey: true })).toEqual({ kind: "search", initial: "A" });
    expect(key("3")).toEqual({ kind: "interval", initial: "3" });
    expect(key("K", { ctrlKey: true })).toEqual({ kind: "search", initial: "" });
    expect(key("k", { ctrlKey: true, metaKey: true })).toBeNull();
    expect(key("v", { ctrlKey: true })).toBeNull();
    expect(key("Escape")).toBeNull();
    expect(key("\u00e9")).toBeNull();
  });
});
