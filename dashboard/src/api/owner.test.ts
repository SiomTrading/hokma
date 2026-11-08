import { beforeEach, describe, expect, it, vi } from "vitest";
import { reloadWithHandoff } from "./handoff";
import { getOwnerNonce, resetOwnerForTests } from "./owner";

describe("owner confirmation of the tab", () => {
  beforeEach(() => {
    resetOwnerForTests();
    sessionStorage.clear();
    window.name = "";
  });

  it("is 128 random bits in memory, stable for the page and new for each page", () => {
    const first = getOwnerNonce();
    expect(first).toMatch(/^[0-9a-f]{32}$/);
    expect(getOwnerNonce()).toBe(first);
    resetOwnerForTests();
    expect(getOwnerNonce()).not.toBe(first);
  });

  it("is not in any storage while the page is open", () => {
    const value = getOwnerNonce();
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain(value);
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(value);
    expect(document.cookie).not.toContain(value);
  });

  it("crosses a reload Hokma makes of the same page through window.name, which is emptied when the next page reads it", () => {
    const value = getOwnerNonce();
    reloadWithHandoff(() => undefined);
    expect(window.name).toContain(value);                                      // only for the instant of the reload
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain(value);
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain(value);
    resetOwnerForTests();                                                      // the next page load
    expect(getOwnerNonce()).toBe(value);
    expect(window.name).toBe("");
  });

  it("never writes the confirmation to sessionStorage or localStorage, not even on its own reload", () => {
    const value = getOwnerNonce();
    reloadWithHandoff(() => undefined);
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.length).toBe(0);
    expect(JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage), document.cookie])).not.toContain(value);
  });

  it("ignores a handed over value that is not a confirmation", () => {
    window.name = 'hokma-handoff:{"n":"<script>"}';
    expect(getOwnerNonce()).toMatch(/^[0-9a-f]{32}$/);
    expect(window.name).toBe("");
  });

  it("ignores a window.name that is not ours and leaves it alone", () => {
    window.name = "someone-else";
    expect(getOwnerNonce()).toMatch(/^[0-9a-f]{32}$/);
    expect(window.name).toBe("someone-else");
    window.name = "";
  });

  it("drops the copy of a page restored from the back/forward cache", () => {
    getOwnerNonce();
    reloadWithHandoff(() => undefined);
    expect(window.name).not.toBe("");
    const restored = new Event("pageshow") as PageTransitionEvent;
    Object.defineProperty(restored, "persisted", { value: true });
    window.dispatchEvent(restored);
    expect(window.name).toBe("");
    vi.restoreAllMocks();
  });
});
