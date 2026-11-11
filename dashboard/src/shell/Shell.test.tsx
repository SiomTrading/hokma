import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { clearToken } from "@/api";
import { setLocale, t } from "@/i18n";
import { renderShell } from "@/test/utils";
import { LAYOUT_KEY } from "./tabsStorage";
import { registerScreen, registerSettingsSection, registerSlot, resetRegistry } from "./registry";
import { useShell } from "./store";
import type { ShellApi } from "./store";

const root = () => screen.getByTestId("shell");

beforeEach(() => resetRegistry());

describe("shell loads", () => {
  it("renders the design layout: top bar, menu, center, right panel, terminal and status bar", () => {
    renderShell();
    expect(root()).toHaveClass("hk");
    expect(root()).not.toHaveClass("light");
    expect(screen.getByRole("complementary", { name: t("nav.menu") })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Proyectos" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Terminal" })).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    expect(screen.getByText("HOKMA")).toBeInTheDocument();
  });

  it("lists every section of the design in the left menu", () => {
    renderShell();
    const menu = screen.getByRole("complementary", { name: t("nav.menu") });
    for (const name of [t("nav.newChat"), t("nav.chat"), t("nav.panel"), t("nav.chart"), t("nav.tasks"), t("nav.learn"), t("nav.settings")]) {
      expect(within(menu).getByRole(name === t("nav.newChat") ? "link" : "button", { name })).toBeInTheDocument();
    }
    expect(within(menu).getByRole("link", { name: t("nav.newChat") })).toHaveAttribute("href", "#/chat/new");
    expect(within(menu).getByText("Ctrl Shift O")).toBeInTheDocument(); // the real shortcut, from SHORTCUTS
    expect(within(menu).getByRole("searchbox", { name: "Buscar conversaciones" })).toBeInTheDocument();
  });

  it("shows the sign-in notice when there is no session token", () => {
    clearToken();
    render(<App />);
    expect(screen.getByRole("heading", { name: t("noToken.title") })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /acceso Hokma/ })).toBeInTheDocument();
    expect(screen.queryByTestId("shell")).not.toBeInTheDocument();
  });

  it("offers to use Hokma in the browser from the entry screen: nonce first, then the call", async () => {
    clearToken();
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return url.endsWith("nonce") ? new Response(JSON.stringify({ nonce: "n" })) : new Response("{}", { status: 202 });
    }));
    render(<App />);
    await userEvent.click(screen.getByTestId("no-token-browser"));
    expect(calls).toEqual(["/api/system/shutdown-nonce", "/api/system/browser"]);
    vi.unstubAllGlobals();
  });

  it("shows the app when a token exists", () => {
    renderShell();
    expect(screen.queryByTestId("no-token")).not.toBeInTheDocument();
  });

  it("speaks English when the locale changes", () => {
    setLocale("en");
    renderShell();
    expect(screen.getByRole("button", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add tab" })).toBeInTheDocument();
  });
});

describe("top bar buttons change the grid like the design", () => {
  beforeEach(() => renderShell());

  it("left menu cycles expanded, icons, hidden", async () => {
    const btn = screen.getByRole("button", { name: new RegExp(t("top.sidebar").split(":")[0]!) });
    await userEvent.click(btn);
    expect(root()).toHaveClass("sbc");
    await userEvent.click(btn);
    expect(root()).toHaveClass("sbx");
    await userEvent.click(btn);
    expect(root()).not.toHaveClass("sbc", "sbx");
  });

  it("the button next to the logo and Ctrl+B change the menu, and the choice is remembered", async () => {
    const side = screen.getByRole("complementary", { name: t("nav.menu") });
    await userEvent.click(within(side).getByRole("button", { name: t("sidebar.collapse") }));
    expect(root()).toHaveClass("sbc");
    expect(JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}").sidebar).toBe(1);
    await userEvent.keyboard("{Control>}b{/Control}");
    expect(root()).toHaveClass("sbx");
  });

  it("terminal toggles the dock", async () => {
    expect(screen.getByRole("region", { name: "Terminal" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Terminal" }));
    expect(screen.queryByRole("region", { name: "Terminal" })).not.toBeInTheDocument();
  });

  it("right panel toggles", async () => {
    await userEvent.click(screen.getByRole("button", { name: "Panel de proyectos" }));
    expect(root()).toHaveClass("rpoff");
    expect(screen.queryByRole("complementary", { name: "Proyectos" })).not.toBeInTheDocument();
  });

  it("full screen hides menu, right panel and terminal", async () => {
    await userEvent.click(screen.getByRole("button", { name: "Pantalla completa" }));
    expect(root()).toHaveClass("focus");
    expect(screen.queryByRole("region", { name: "Terminal" })).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Proyectos" })).not.toBeInTheDocument();
  });
});

describe("tabs and screens", () => {
  it("opens, activates and closes tabs, and shows the closed view", async () => {
    renderShell();
    const strip = () => screen.getAllByRole("button", { name: /^Cerrar (?!terminal)/ });
    expect(strip()).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: t("nav.chart") }));
    expect(strip()).toHaveLength(3);
    for (const close of strip().reverse()) await userEvent.click(close);
    expect(screen.getByText("Todo cerrado")).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole("main")).getByRole("button", { name: t("nav.newChat") }));
    expect(screen.queryByText("Todo cerrado")).not.toBeInTheDocument();
  });

  it("shows a neutral state for a screen nobody registered", () => {
    renderShell();
    expect(within(screen.getByRole("main")).getByText(t("screen.unavailable.title"))).toBeInTheDocument();
  });

  it("renders a registered screen with its params and title", async () => {
    registerScreen({
      id: "chart",
      title: (params) => `Chart ${params.symbol ?? ""}`.trim(),
      component: ({ params }) => <section aria-label="chart-screen">symbol:{params.symbol ?? "none"}</section>,
    });
    function Opener() {
      const shell = useShell();
      return <button onClick={() => shell.openScreen("chart", { symbol: "ABC" })}>open-chart</button>;
    }
    registerSlot("statusBar", Opener);
    renderShell();
    await userEvent.click(screen.getByText("open-chart"));
    expect(screen.getByLabelText("chart-screen")).toHaveTextContent("symbol:ABC");
    expect(screen.getByRole("button", { name: "Chart ABC" })).toBeInTheDocument();
  });

  it("persists tabs and layout across reloads", async () => {
    const first = renderShell();
    await userEvent.click(screen.getByRole("button", { name: "Tareas programadas" }));
    await userEvent.click(screen.getByRole("button", { name: "Terminal" }));
    first.unmount();
    renderShell();
    expect(screen.getAllByRole("button", { name: /^Cerrar (?!terminal)/ })).toHaveLength(3);
    expect(screen.queryByRole("region", { name: "Terminal" })).not.toBeInTheDocument();
  });

  it("add menu lists the modules and opens one", async () => {
    renderShell();
    await userEvent.click(screen.getByRole("button", { name: t("top.addTab") }));
    const menu = screen.getByRole("menu", { name: t("top.addTab") });
    expect(within(menu).getByText(t("addMenu.modules"))).toBeInTheDocument();
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Aprende" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cerrar Aprende" })).toBeInTheDocument();
  });
});

describe("slots and settings", () => {
  it("fills a slot and survives a broken one", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    registerSlot("brand.subtitle", () => <span>fund-name</span>);
    registerSlot("statusBar", () => {
      throw new Error("boom");
    });
    renderShell();
    expect(screen.getByText("fund-name")).toBeInTheDocument();
    expect(screen.getByTestId("shell")).toBeInTheDocument();
    error.mockRestore();
  });

  it("settings opens from the menu with all sections, switches and closes with Escape", async () => {
    registerSettingsSection({ id: "general", component: () => <p>general-body</p> });
    renderShell();
    await userEvent.click(screen.getByRole("button", { name: "Ajustes" }));
    const dialog = screen.getByRole("dialog", { name: "Ajustes" });
    for (const name of ["General", "Agentes", "Modelos y consumo", "Extensiones", "Estado del sistema", "Novedades"]) {
      expect(within(dialog).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(dialog).getByText("general-body")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Agentes" }));
    expect(within(dialog).getByText(t("settings.unavailable"))).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("keyboard shortcuts", () => {
  const press = (init: KeyboardEventInit) => fireEvent.keyDown(document, init);

  it("Ctrl Shift O opens a new draft conversation in the same browser tab; Ctrl N does nothing", () => {
    let shellApi: ShellApi | null = null;
    function Probe() {
      shellApi = useShell();
      return null;
    }
    registerSlot("statusBar", Probe);
    renderShell();
    const drafts = () => shellApi!.tabs.filter((tab) => tab.params.draft).length;
    const before = drafts();
    press({ key: "n", ctrlKey: true });
    expect(drafts()).toBe(before);
    press({ key: "O", ctrlKey: true, shiftKey: true });
    expect(drafts()).toBe(before + 1);
    expect(shellApi!.activeKey).toContain("chat:draft=");
  });

  it("Ctrl B, Ctrl J, Ctrl ` and Ctrl Shift F drive the layout", () => {
    renderShell();
    press({ key: "b", ctrlKey: true });
    expect(root()).toHaveClass("sbc");
    press({ key: "j", ctrlKey: true });
    expect(root()).toHaveClass("rpoff");
    press({ key: "`", code: "Backquote", ctrlKey: true });
    expect(screen.queryByRole("region", { name: "Terminal" })).not.toBeInTheDocument();
    press({ key: "F", ctrlKey: true, shiftKey: true });
    expect(root()).toHaveClass("focus");
  });

  it("Ctrl K expands the menu and focuses the search box", async () => {
    renderShell();
    press({ key: "b", ctrlKey: true });
    press({ key: "b", ctrlKey: true });
    expect(root()).toHaveClass("sbx");
    await act(async () => {
      press({ key: "k", ctrlKey: true });
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    expect(root()).not.toHaveClass("sbx");
    expect(screen.getByRole("searchbox")).toHaveFocus();
  });

  it("Ctrl , opens settings and Escape closes it", () => {
    renderShell();
    press({ key: ",", ctrlKey: true });
    expect(screen.getByRole("dialog", { name: "Ajustes" })).toBeInTheDocument();
    press({ key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("ignores keys without the modifier", () => {
    renderShell();
    press({ key: "b" });
    expect(root()).not.toHaveClass("sbc");
  });
});

describe("theme", () => {
  it("switches to light and persists it", async () => {
    function Toggler() {
      const shell = useShell();
      return <button onClick={() => shell.setTheme("light")}>go-light</button>;
    }
    registerSlot("statusBar", Toggler);
    renderShell();
    await userEvent.click(screen.getByText("go-light"));
    expect(root()).toHaveClass("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("hokma.theme")).toBe("light");
  });
});
