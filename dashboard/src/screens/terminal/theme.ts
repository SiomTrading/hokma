import type { ITheme } from "@xterm/xterm";

/** xterm colors taken from the design tokens of the element that hosts the terminal (dark or light theme). */
export function readTheme(from: Element | null): ITheme {
  const style = getComputedStyle(from ?? document.documentElement);
  const v = (name: string, fallback: string): string => style.getPropertyValue(name).trim() || fallback;
  const light = !!from?.closest(".light");
  const t1 = v("--t1", light ? "#101516" : "#f0f5f8");
  const acc = v("--acc", "#00cccc");
  return {
    background: "rgba(0, 0, 0, 0)",
    foreground: t1,
    cursor: acc,
    cursorAccent: v("--bg", light ? "#f0f5f8" : "#101516"),
    selectionBackground: v("--acc-sub", "rgba(0, 204, 204, 0.25)"),
    black: light ? "#101516" : "#3a4549",
    red: v("--bad", "#ff8a73"),
    green: v("--ok", "#4fd69c"),
    yellow: v("--warn", "#f2a65a"),
    blue: acc,
    magenta: v("--org", "#f26038"),
    cyan: v("--acc-h", "#33dada"),
    white: v("--t2", "#a8b3b8"),
    brightBlack: v("--t3", "#8a969c"),
    brightRed: v("--bad", "#ff8a73"),
    brightGreen: v("--ok", "#4fd69c"),
    brightYellow: v("--warn", "#f2a65a"),
    brightBlue: v("--acc-h", "#33dada"),
    brightMagenta: v("--org", "#f26038"),
    brightCyan: v("--acc-h", "#33dada"),
    brightWhite: t1,
  };
}
