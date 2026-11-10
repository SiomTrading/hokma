import { describe, expect, it } from "vitest";
import { SHORTCUTS, formatShortcut, formatShortcutCompact, getShortcut, matchShortcut } from "./shortcuts";

const ev = (init: Partial<KeyboardEvent>) => ({ key: "", code: "", ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...init });

describe("shortcuts", () => {
  it("formats labels for Windows/Linux and macOS", () => {
    expect(formatShortcut(getShortcut("newChat"), false)).toBe("Ctrl Shift O");
    expect(formatShortcut(getShortcut("newChat"), true)).toBe("Cmd Shift O");
    expect(formatShortcut(getShortcut("fullscreen"), false)).toBe("Ctrl Shift F");
    expect(formatShortcut(getShortcut("escape"), false)).toBe("Esc");
    expect(formatShortcutCompact(getShortcut("newChat"), false)).toBe("Ctrl⇧O");
    expect(formatShortcutCompact(getShortcut("newChat"), true)).toBe("⌘⇧O");
  });

  it("matches every defined shortcut on Windows/Linux", () => {
    expect(matchShortcut(ev({ key: "O", ctrlKey: true, shiftKey: true }), false)).toBe("newChat");
    expect(matchShortcut(ev({ key: "k", ctrlKey: true }), false)).toBe("search");
    expect(matchShortcut(ev({ key: "`", code: "Backquote", ctrlKey: true }), false)).toBe("terminal");
    expect(matchShortcut(ev({ key: "b", ctrlKey: true }), false)).toBe("sidebar");
    expect(matchShortcut(ev({ key: "j", ctrlKey: true }), false)).toBe("rightPanel");
    expect(matchShortcut(ev({ key: "F", ctrlKey: true, shiftKey: true }), false)).toBe("fullscreen");
    expect(matchShortcut(ev({ key: ",", ctrlKey: true }), false)).toBe("settings");
  });

  it("uses Cmd on macOS and ignores Ctrl there", () => {
    expect(matchShortcut(ev({ key: "o", metaKey: true, shiftKey: true }), true)).toBe("newChat");
    expect(matchShortcut(ev({ key: "o", ctrlKey: true, shiftKey: true }), true)).toBeNull();
  });

  it("does not confuse Shift variants or Alt combos", () => {
    expect(matchShortcut(ev({ key: "f", ctrlKey: true }), false)).toBeNull();
    expect(matchShortcut(ev({ key: "n", ctrlKey: true, shiftKey: true }), false)).toBeNull();
    expect(matchShortcut(ev({ key: "n", ctrlKey: true, altKey: true }), false)).toBeNull();
  });

  it("U-10: Ctrl+N is the browser's and gives nothing; no two shortcuts share a key combination", () => {
    expect(matchShortcut(ev({ key: "n", ctrlKey: true }), false)).toBeNull();
    expect(matchShortcut(ev({ key: "n", metaKey: true }), true)).toBeNull();
    expect(matchShortcut(ev({ key: "o", ctrlKey: true }), false)).toBeNull(); // without Shift it is not ours
    const combos = SHORTCUTS.map((s) => `${s.key}|${s.mod}|${!!s.shift}`);
    expect(new Set(combos).size).toBe(combos.length);
  });

  it("every shortcut has a translated label", () => {
    expect(SHORTCUTS.length).toBe(8);
    for (const s of SHORTCUTS) expect(s.labelKey).toMatch(/^shortcut\./);
  });
});
