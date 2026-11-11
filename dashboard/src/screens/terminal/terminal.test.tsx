import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithApp } from "@/test/utils";
import { useShell } from "@/shell/useShell";
import { TerminalPanel } from "./TerminalPanel";
import { handleKey, resetTerminals, setTerminalDrivers } from "./store";

interface FakeSocket {
  path: string;
  handlers: { onMessage?: (d: unknown) => void; onClose?: (e: unknown) => void };
  sent: unknown[];
  closed: boolean;
}

const sockets: FakeSocket[] = [];
const terminals: FakeTerm[] = [];

class FakeTerm {
  cols = 100;
  rows = 8;
  options: Record<string, unknown> = {};
  written: string[] = [];
  disposed = false;
  selection = "";
  dataCb: ((d: string) => void) | null = null;
  resizeCb: (() => void) | null = null;
  constructor(opts: Record<string, unknown>) {
    this.options = { ...opts };
    terminals.push(this);
  }
  loadAddon() {}
  open(host: HTMLElement) {
    const el = document.createElement("div");
    el.className = "xterm";
    host.appendChild(el);
  }
  write(text: string) {
    this.written.push(text);
  }
  onData(cb: (d: string) => void) {
    this.dataCb = cb;
  }
  onResize(cb: () => void) {
    this.resizeCb = cb;
  }
  attachCustomKeyEventHandler() {}
  dispose() {
    this.disposed = true;
  }
  focus() {}
  hasSelection() {
    return this.selection !== "";
  }
  getSelection() {
    return this.selection;
  }
}

function fakeOpenSocket(path: string, handlers: FakeSocket["handlers"]) {
  const sock: FakeSocket = { path, handlers, sent: [], closed: false };
  sockets.push(sock);
  return {
    send: (d: unknown) => {
      sock.sent.push(d);
      return true;
    },
    close: () => {
      sock.closed = true;
    },
  };
}

function Harness() {
  const shell = useShell();
  return (
    <>
      <button onClick={() => shell.emit("terminal:new")}>plus</button>
      <button onClick={() => shell.emit("project:active", { id: 7 })}>project</button>
      <TerminalPanel />
    </>
  );
}

const frame = (s: FakeSocket, f: unknown) => act(() => s.handlers.onMessage?.(f));

beforeEach(() => {
  setTerminalDrivers({ Terminal: FakeTerm as never, FitAddon: class { fit() {} } as never, openSocket: fakeOpenSocket as never });
  sockets.length = 0;
  terminals.length = 0;
  globalThis.ResizeObserver = class {
    observe() {}
    disconnect() {}
    unobserve() {}
  } as unknown as typeof ResizeObserver;
});
afterEach(() => {
  act(() => resetTerminals());
  setTerminalDrivers(null);
});

describe("terminal panel", () => {
  it("opens one real session on first show and wires input, output and resize", () => {
    renderWithApp(<Harness />);
    expect(sockets).toHaveLength(1);
    expect(sockets[0].path).toMatch(/^\/ws\/terminal\?cols=100&rows=8$/);
    frame(sockets[0], { type: "ready", pid: 1, cwd: "/work" });
    expect(sockets[0].sent).toContainEqual({ type: "resize", cols: 100, rows: 8 });
    frame(sockets[0], { type: "output", data: "hola\r\n" });
    expect(terminals[0].written).toContain("hola\r\n");
    terminals[0].dataCb?.("ls\r");
    expect(sockets[0].sent).toContainEqual({ type: "input", data: "ls\r" });
    terminals[0].cols = 120;
    terminals[0].resizeCb?.();
    expect(sockets[0].sent).toContainEqual({ type: "resize", cols: 120, rows: 8 });
  });

  it("attaches the xterm element to the panel", () => {
    const { container } = renderWithApp(<Harness />);
    expect(container.querySelector(".term-area .term-host .xterm")).toBeInTheDocument();
  });

  it("opens more tabs from the dock plus button event and closes them cleanly", async () => {
    renderWithApp(<Harness />);
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("plus"));
    expect(sockets).toHaveLength(2);
    expect(screen.getByRole("tablist", { name: "Terminales abiertas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Terminal 2" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Terminal 1" }));
    expect(screen.getByRole("button", { name: "Terminal 1" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Cerrar terminal 2" }));
    expect(sockets[1].closed).toBe(true);
    expect(terminals[1].disposed).toBe(true);
    expect(sockets[0].closed).toBe(false);
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("closing the last tab shows the empty state and a new terminal can be opened", async () => {
    renderWithApp(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Cerrar terminal 1" }));
    expect(sockets[0].closed).toBe(true);
    expect(screen.getByText("No hay terminales abiertas")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Nueva terminal" }));
    expect(sockets).toHaveLength(2);
  });

  it("keeps the shells alive when the dock is hidden and shows them again", () => {
    const { unmount } = renderWithApp(<Harness />);
    unmount();
    expect(sockets[0].closed).toBe(false);
    expect(terminals[0].disposed).toBe(false);
    const again = renderWithApp(<Harness />);
    expect(sockets).toHaveLength(1);
    expect(again.container.querySelector(".term-host .xterm")).toBeInTheDocument();
  });

  it("opens in the active project's folder", async () => {
    renderWithApp(<Harness />);
    await userEvent.click(screen.getByText("project"));
    await userEvent.click(screen.getByText("plus"));
    expect(sockets[1].path).toContain("project_id=7");
    expect(sockets[0].path).not.toContain("project_id");
  });

  it("tells the user when the process ends or the server refuses", () => {
    renderWithApp(<Harness />);
    frame(sockets[0], { type: "exit", code: 3 });
    expect(terminals[0].written.join("")).toMatch(/El proceso termin. \(c.digo 3\)/);
    act(() => sockets[0].handlers.onClose?.({ code: 1000 }));
    frame(sockets[0], { type: "error", code: "too_many_terminals" });
    expect(terminals[0].written.join("")).toContain("demasiadas terminales");
  });

  it("shows the real reason when the shell cannot start (task 3174)", () => {
    renderWithApp(<Harness />);
    frame(sockets[0], { type: "error", code: "spawn_failed", detail: "pwsh.exe (full environment): WinptyError: access denied\ncmd.exe: boom" });
    const text = terminals[0].written.join("");
    expect(text).toContain("No se pudo iniciar la terminal.");
    expect(text).toContain("WinptyError: access denied\r\ncmd.exe: boom");
  });

  it("reports a lost connection", () => {
    renderWithApp(<Harness />);
    frame(sockets[0], { type: "ready", pid: 1, cwd: "/w" });
    act(() => sockets[0].handlers.onClose?.({ code: 1006 }));
    expect(terminals[0].written.join("")).toMatch(/Se perdi. la conexi.n/);
  });

  it("types the launch command once the shell is ready", async () => {
    const { launchTerminal } = await import("./store");
    renderWithApp(<Harness />);
    act(() => {
      launchTerminal({ command: "claude" });
    });
    expect(sockets[1].sent).toEqual([]);
    frame(sockets[1], { type: "ready", pid: 2, cwd: "/w" });
    expect(sockets[1].sent).toContainEqual({ type: "input", data: "claude\r" });
  });
});

describe("keys", () => {
  const term = () => new FakeTerm({}) as unknown as import("@xterm/xterm").Terminal;
  const key = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);
  let written: string[];
  beforeEach(() => {
    written = [];
    Object.defineProperty(navigator, "clipboard", { value: { writeText: (s: string) => (written.push(s), Promise.resolve()) }, configurable: true });
  });

  it("Ctrl Shift C copies the selection, Ctrl Shift V is left to the browser paste", () => {
    const t = term() as unknown as FakeTerm;
    t.selection = "abc";
    expect(handleKey(t as never, key({ key: "C", ctrlKey: true, shiftKey: true }), false)).toBe(false);
    expect(written).toEqual(["abc"]);
    expect(handleKey(t as never, key({ key: "V", ctrlKey: true, shiftKey: true }), false)).toBe(false);
  });

  it("Ctrl C copies only with a selection; otherwise it reaches the shell as an interrupt", () => {
    const t = term() as unknown as FakeTerm;
    expect(handleKey(t as never, key({ key: "c", ctrlKey: true }), false)).toBe(true);
    t.selection = "x";
    expect(handleKey(t as never, key({ key: "c", ctrlKey: true }), false)).toBe(false);
    expect(written).toEqual(["x"]);
  });

  it("Cmd C and Cmd V on macOS", () => {
    const t = term() as unknown as FakeTerm;
    t.selection = "m";
    expect(handleKey(t as never, key({ key: "c", metaKey: true }), true)).toBe(false);
    expect(handleKey(t as never, key({ key: "v", metaKey: true }), true)).toBe(false);
    expect(written).toEqual(["m"]);
  });

  it("the terminal toggle goes to the app, readline keys stay in the shell", () => {
    const t = term();
    expect(handleKey(t, key({ key: "`", code: "Backquote", ctrlKey: true }), false)).toBe(false);
    const ctrlK = key({ key: "k", ctrlKey: true });
    const stop = vi.spyOn(ctrlK, "stopPropagation");
    expect(handleKey(t, ctrlK, false)).toBe(true);
    expect(stop).toHaveBeenCalled();
  });
});
