import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { setToken } from "@/api";
import { t } from "@/i18n";
import { renderShell, TEST_TOKEN } from "@/test/utils";
import { parseChatHash } from "./chatRoute";
import { resetRegistry } from "./registry";
import { ShellProvider } from "./store";
import type { ShellApi } from "./store";
import { useShell } from "./useShell";
import { LAST_TABS_KEY, LAYOUT_KEY, LEGACY_KEY, TABS_KEY, startupTabs } from "./tabsStorage";

let api: ShellApi;
function Probe() {
  api = useShell();
  return null;
}
const mount = () => render(<ShellProvider><Probe /></ShellProvider>);
const drafts = () => api.tabs.filter((tab) => tab.params.draft);

beforeEach(() => {
  resetRegistry();
  window.history.replaceState(null, "", "/");
});

describe("U-01 / U-02: a new conversation is a new tab", () => {
  it("two calls in a row give two different drafts, the last one in front", () => {
    mount();
    const before = drafts().length;
    act(() => api.newConversation());
    act(() => api.newConversation());
    expect(drafts()).toHaveLength(before + 2);
    const [a, b] = drafts().slice(-2);
    expect(a!.params.draft).not.toBe(b!.params.draft);
    expect(api.activeKey).toBe(b!.key);
  });

  it("from a stored conversation it opens a draft and leaves the conversation open", () => {
    mount();
    act(() => api.openScreen("chat", { conversation: "42" }));
    act(() => api.newConversation());
    expect(api.tabs.some((tab) => tab.key === "chat:conversation=42")).toBe(true);
    expect(api.activeKey).toContain("chat:draft=");
  });

  it("E-03 side: the Chat entry goes to the chat tab used last and never empties one", () => {
    mount();
    act(() => api.openScreen("chat", { conversation: "42" }));
    act(() => api.openScreen("panel"));
    const count = api.tabs.length;
    act(() => api.openScreen("chat"));
    expect(api.activeKey).toBe("chat:conversation=42");
    expect(api.tabs).toHaveLength(count);
  });
});

describe("U-03 / U-04: binding a conversation to a draft", () => {
  it("keeps the key, activates the same tab when the list asks for it, and survives a reload as a conversation", () => {
    const first = mount();
    act(() => api.newConversation());
    const key = api.activeKey!;
    const count = api.tabs.length;
    act(() => api.bindConversation(key, "77"));
    expect(api.tabs.find((tab) => tab.key === key)).toMatchObject({ key, conversation: "77" });
    act(() => api.openScreen("panel"));
    act(() => api.openScreen("chat", { conversation: "77" }));
    expect(api.activeKey).toBe(key);
    expect(api.tabs).toHaveLength(count);

    const saved = JSON.parse(sessionStorage.getItem(TABS_KEY)!) as { tabs: Array<{ key: string; params: Record<string, string> }> };
    expect(saved.tabs.map((tab) => tab.key)).toContain("chat:conversation=77");
    expect(saved.tabs.some((tab) => tab.key === key)).toBe(false);

    first.unmount();
    mount();
    expect(api.tabs.some((tab) => tab.key === "chat:conversation=77")).toBe(true);
    expect(api.activeKey).toBe("chat:conversation=77");
  });
});

describe("U-05: chat addresses", () => {
  it("accepts #/chat/new and #/chat/<digits> and nothing else", () => {
    expect(parseChatHash("#/chat/new")).toEqual({ kind: "new" });
    expect(parseChatHash("#/chat/42")).toEqual({ kind: "conversation", id: "42" });
    for (const bad of ["#/chat/abc", "#/chat/", "#t=x", "#/connections", "#/chat/42/x", "#/chat/1234567890123456789", ""]) {
      expect(parseChatHash(bad), bad).toBeNull();
    }
  });
});

describe("U-06: how a browser tab starts", () => {
  const lastPicture = [
    { key: "panel", screen: "panel", params: {} },
    { key: "chat:conversation=5", screen: "chat", params: { conversation: "5" } },
    { key: "chat:draft=old", screen: "chat", params: { draft: "old" } },
  ];
  const own = {
    tabs: [
      { key: "chat:conversation=1", screen: "chat", params: { conversation: "1" } },
      { key: "chat:draft=mine", screen: "chat", params: { draft: "mine" } },
    ],
    activeKey: "chat:draft=mine",
  };

  it("F5 with its own state and no hash restores it", () => {
    sessionStorage.setItem(TABS_KEY, JSON.stringify(own));
    const start = startupTabs("");
    expect(start.tabs.map((tab) => tab.key)).toEqual(["chat:conversation=1", "chat:draft=mine"]);
    expect(start.activeKey).toBe("chat:draft=mine");
  });

  it("F5 on #/chat/new: an empty active draft stays, otherwise a new draft is added", () => {
    sessionStorage.setItem(TABS_KEY, JSON.stringify(own));
    const stays = startupTabs("#/chat/new");
    expect(stays.tabs).toHaveLength(2);
    expect(stays.activeKey).toBe("chat:draft=mine");
    sessionStorage.setItem(TABS_KEY, JSON.stringify({ ...own, activeKey: "chat:conversation=1" }));
    const added = startupTabs("#/chat/new");
    expect(added.tabs).toHaveLength(3);
    expect(added.activeKey).toContain("chat:draft=");
    expect(added.activeKey).not.toBe("chat:draft=mine");
  });

  it("F5 on #/chat/<id> activates that conversation, or opens it when it was not there", () => {
    sessionStorage.setItem(TABS_KEY, JSON.stringify(own));
    expect(startupTabs("#/chat/1").activeKey).toBe("chat:conversation=1");
    const opened = startupTabs("#/chat/9");
    expect(opened.activeKey).toBe("chat:conversation=9");
    expect(opened.tabs).toHaveLength(3);
  });

  it("a new browser tab from a link (empty session) gets only what the address names", () => {
    localStorage.setItem(LAST_TABS_KEY, JSON.stringify(lastPicture));
    const fresh = startupTabs("#/chat/new");
    expect(fresh.tabs).toHaveLength(1);
    expect(fresh.tabs[0]!.params.draft).toBeTruthy();
    expect(startupTabs("#/chat/12").tabs.map((tab) => tab.key)).toEqual(["chat:conversation=12"]);
  });

  it("a new browser tab from the icon (no hash) gets the last picture without drafts, plus one new active draft", () => {
    localStorage.setItem(LAST_TABS_KEY, JSON.stringify(lastPicture));
    const start = startupTabs("");
    expect(start.tabs.map((tab) => tab.key).slice(0, 2)).toEqual(["panel", "chat:conversation=5"]);
    expect(start.tabs).toHaveLength(3);
    expect(start.tabs[2]!.params.draft).toBeTruthy();
    expect(start.tabs[2]!.params.draft).not.toBe("old");
    expect(start.activeKey).toBe(start.tabs[2]!.key);
  });

  it("two browser tabs do not step on each other: the layout is shared, the tabs are not", () => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ sidebar: 1, rightPanel: false, terminal: false }));
    sessionStorage.setItem(TABS_KEY, JSON.stringify(own));
    const a = startupTabs("");
    sessionStorage.clear(); // another browser tab has its own sessionStorage
    const b = startupTabs("#/chat/3");
    expect(a.layout).toEqual({ sidebar: 1, rightPanel: false, terminal: false });
    expect(b.layout).toEqual(a.layout);
    expect(b.tabs.map((tab) => tab.key)).toEqual(["chat:conversation=3"]);
    expect(a.tabs.map((tab) => tab.key)).not.toContain("chat:conversation=3");
  });
});

describe("U-07: the old single key", () => {
  it("fills the two new keys once, is removed, and a plain chat tab becomes a draft", () => {
    localStorage.setItem(
      LEGACY_KEY,
      JSON.stringify({
        tabs: [
          { key: "chat", screen: "chat", params: {} },
          { key: "chat:conversation=8", screen: "chat", params: { conversation: "8" } },
          { key: "panel", screen: "panel", params: {} },
        ],
        activeKey: "chat",
        sidebar: 2,
        rightPanel: false,
        terminal: true,
      }),
    );
    const start = startupTabs("");
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
    expect(JSON.parse(localStorage.getItem(LAYOUT_KEY)!)).toEqual({ sidebar: 2, rightPanel: false, terminal: true });
    const picture = JSON.parse(localStorage.getItem(LAST_TABS_KEY)!) as Array<{ key: string }>;
    expect(picture.some((tab) => tab.key === "chat")).toBe(false);
    expect(picture.some((tab) => tab.key.startsWith("chat:draft="))).toBe(true);
    expect(start.layout.sidebar).toBe(2);
    expect(start.tabs.map((tab) => tab.key)).toContain("chat:conversation=8");
    expect(start.tabs.some((tab) => tab.key === "chat")).toBe(false);
  });
});

describe("U-08: nothing secret in the storages", () => {
  it("the session token never reaches sessionStorage or localStorage", () => {
    setToken(TEST_TOKEN);
    renderShell();
    fireEvent.click(screen.getByRole("link", { name: t("nav.newChat") }));
    const dump = JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage)]);
    expect(dump).not.toContain(TEST_TOKEN);
    expect(sessionStorage.getItem(TABS_KEY)).not.toBeNull();
  });
});

describe("U-09: links open in another tab with a modifier or the wheel", () => {
  it("a plain click is handled by the page; Ctrl, Cmd, Shift and the middle button are left to the browser", () => {
    renderShell();
    const link = screen.getByRole("link", { name: t("nav.newChat") });
    expect(link).toHaveAttribute("href", "#/chat/new");
    const prevented = (init: MouseEventInit) => !fireEvent.click(link, init);
    expect(prevented({ button: 0 })).toBe(true);
    expect(prevented({ button: 0, ctrlKey: true })).toBe(false);
    expect(prevented({ button: 0, metaKey: true })).toBe(false);
    expect(prevented({ button: 0, shiftKey: true })).toBe(false);
    expect(prevented({ button: 1 })).toBe(false);
  });

  it("the title promises another tab only where the session is remembered", () => {
    renderShell();
    const menu = screen.getByRole("complementary", { name: t("nav.menu") });
    const link = within(menu).getByRole("link", { name: t("nav.newChat") });
    expect(link.getAttribute("title")).toMatch(/Ctrl\+clic/);
  });
});
