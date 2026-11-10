import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { t } from "@/i18n";
import { renderShell } from "@/test/utils";
import { resetRegistry } from "./registry";
import { requiredSidebarHeight, sidebarDensity } from "./sidebarDensity";

// Fixed rows of the shell grid (shell.css): top bar, status bar, Terminal panel, and the minimum height of the app.
const TOP_BAR = 42;
const STATUS_BAR = 28;
const TERMINAL = 170;
const APP_MIN_HEIGHT = 480;

/** The height the left menu gets in a window of `viewport` px, with or without the Terminal open. */
const menuHeight = (viewport: number, terminal: boolean) => Math.max(viewport, APP_MIN_HEIGHT) - TOP_BAR - STATUS_BAR - (terminal ? TERMINAL : 0);

let height = 0;
let observers: Array<() => void> = [];
const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
const originalObserver = globalThis.ResizeObserver;

beforeEach(() => {
  resetRegistry();
  observers = [];
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("side") ? height : 0;
    },
  });
  globalThis.ResizeObserver = class {
    constructor(private cb: () => void) {
      observers.push(() => this.cb());
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

afterEach(() => {
  if (original) Object.defineProperty(HTMLElement.prototype, "clientHeight", original);
  globalThis.ResizeObserver = originalObserver;
});

const menu = () => screen.getByRole("complementary", { name: t("nav.menu") });

describe("left menu by height (task 3137)", () => {
  for (const viewport of [600, 700, 768]) {
    for (const terminal of [false, true]) {
      it(`${viewport}px ${terminal ? "with" : "without"} Terminal: Settings and Shut down fit and are reachable`, () => {
        height = menuHeight(viewport, terminal);
        renderShell();
        const density = sidebarDensity(height);
        expect(menu()).toHaveAttribute("data-density", density);
        expect(requiredSidebarHeight(density)).toBeLessThanOrEqual(height);
        expect(within(menu()).getByRole("button", { name: t("nav.settings") })).toBeVisible();
        expect(within(menu()).getByRole("button", { name: t("nav.shutdown") })).toBeVisible();
        expect(within(menu()).getByRole("button", { name: t("nav.learn") })).toBeVisible();
      });
    }
  }

  it("1366x768 laptop with the Terminal open folds Recents away instead of cutting the bottom", () => {
    height = menuHeight(650, true); // 768 px screen minus the browser bars
    renderShell();
    expect(menu()).toHaveAttribute("data-density", "tight");
  });

  it("follows the menu height live: opening the Terminal tightens it, closing it restores the design", () => {
    height = menuHeight(768, false);
    renderShell();
    expect(menu()).toHaveAttribute("data-density", "normal");
    height = menuHeight(768, true);
    act(() => observers.forEach((fire) => fire()));
    expect(menu()).toHaveAttribute("data-density", "compact");
    height = menuHeight(600, true);
    act(() => observers.forEach((fire) => fire()));
    expect(menu()).toHaveAttribute("data-density", "tight");
    height = menuHeight(768, false);
    act(() => observers.forEach((fire) => fire()));
    expect(menu()).toHaveAttribute("data-density", "normal");
  });

  it("levels go from roomy to tight and an unmeasured menu keeps the design", () => {
    expect(requiredSidebarHeight("normal")).toBeGreaterThan(requiredSidebarHeight("compact"));
    expect(requiredSidebarHeight("compact")).toBeGreaterThan(requiredSidebarHeight("tight"));
    expect(sidebarDensity(0)).toBe("normal");
    expect(sidebarDensity(2000)).toBe("normal");
    expect(sidebarDensity(requiredSidebarHeight("compact"))).toBe("compact");
    expect(sidebarDensity(requiredSidebarHeight("compact") - 1)).toBe("tight");
  });
});

describe("New conversation shortcut label (task 3137)", () => {
  it("shows the real shortcut from SHORTCUTS, not Ctrl N", () => {
    renderShell();
    const link = within(menu()).getByRole("link", { name: t("nav.newChat") });
    expect(within(link).getByText("Ctrl Shift O")).toBeInTheDocument();
    expect(within(menu()).queryByText(/Ctrl ?N\b/)).toBeNull();
  });
});
