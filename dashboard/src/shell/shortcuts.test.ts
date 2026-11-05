import { describe, expect, it } from "vitest";
import { SHORTCUTS, formatShortcut, getShortcut, matchShortcut } from "./shortcuts";

const ev = (init: Partial<KeyboardEvent>) => ({ key: "", code: "", ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...init });

describe("shortcuts", () => {
  it("formats labels for Windows/Linux and macOS", () => {
    expect(formatShortcut(getShortcut("newChat"), false)).toBe("Ctrl N");
    expect(formatShortcut(getShortcut("newChat"), true)).toBe("Cmd N");
    expect(formatShortcut(getShortcut("fullscreen"), false)).toBe("Ctrl Shift F");
    expect(formatShortcut(getShortcut("escape"), false)).toBe("Esc");
  });

  it("matches every defined shortcut on Windows/Linux", () => {
    expect(matchShortcut(ev({ key: "n", ctrlKey: true }), false)).toBe("newChat");
    expect(matchShortcut(ev({ key: "k", ctrlKey: true }), false)).toBe("search");
    expect(matchShortcut(ev({ key: "`", code: "Backquote", ctrlKey: true }), false)).toBe("terminal");
    expect(matchShortcut(ev({ key: "b", ctrlKey: true }), false)).toBe("sidebar");
    expect(matchShortcut(ev({ key: "j", ctrlKey: true }), false)).toBe("rightPanel");
    expect(matchShortcut(ev({ key: "F", ctrlKey: true, shiftKey: true }), false)).toBe("fullscreen");
    expect(matchShortcut(ev({ key: ",", ctrlKey: true }), false)).toBe("settings");
  });

  it("uses Cmd on macOS and ignores Ctrl there", () => {
    expect(matchShortcut(ev({ key: "n", metaKey: true }), true)).toBe("newChat");
    expect(matchShortcut(ev({ key: "n", ctrlKey: true }), true)).toBeNull();
  });

  it("does not confuse Shift variants or Alt combos", () => {
    expect(matchShortcut(ev({ key: "f", ctrlKey: true }), false)).toBeNull();
    expect(matchShortcut(ev({ key: "n", ctrlKey: true, shiftKey: true }), false)).toBeNull();
    expect(matchShortcut(ev({ key: "n", ctrlKey: true, altKey: true }), false)).toBeNull();
  });

  it("every shortcut has a translated label", () => {
    expect(SHORTCUTS.length).toBe(8);
    for (const s of SHORTCUTS) expect(s.labelKey).toMatch(/^shortcut\./);
  });
});
